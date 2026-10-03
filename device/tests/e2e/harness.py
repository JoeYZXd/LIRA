"""U9 e2e 全 mock 装配（核心交付）：把 main.py 的装配图做成可注入的整体设备。

装配图（计划 U9 Approach「mock AudioIO 注入 wav / LLM 本地 stub / 后台
TestClient 起真实 app」）::

    WavQueueSource(mock mic, wav 注入)
      → PrivacyGatedSink（隐私门，R12 fail-closed）
        → 按状态机路由分发：
            WAKE     → 真实 KWS（小丽拉）→ engine.on_wake()
            LISTEN   → 真实流式 ASR → endpoint → engine.on_asr_text()
            PLAYBACK → 真实白名单 KWS → engine.on_asr_text()
    DialogEngine（U3 状态机，虚拟时钟 tick）
      ├─ speak            → speak_log（断言面）+ 可选 TTS 合成留档
      ├─ send_ir          → IRService（第二道安全闸）→ MockIrController
      ├─ start_capture    → ReadingPipeline（FakeCamera + FakeOcrEngine
      │                     + 可选 text_polisher=LLM 白话钩子）
      └─ playback_*       → ReadingSession（PacedBlockSpeaker 计块播放）
    SyncClient（真实设备端同步）→ on_snapshot_applied → 禁用播报（AE7）
    LlmClient（httpx2.MockTransport stub，网络层计数 = AE1 零请求断言面）

时钟纪律：状态机超时（R25/R23）走**虚拟时钟**——每注入 0.1s 音频推进
0.1s 虚拟时间，测试可瞬间快进 10s 确认窗口而无需真实等待。
"""

from __future__ import annotations

import asyncio
import json
import time
import wave
from pathlib import Path

import cv2
import numpy as np

from lira.appliances.ir import ApplianceError, IRService
from lira.appliances.store import ApplianceStore
from lira.audio._paths import SAMPLE_RATE
from lira.audio.asr import AsrStream, StreamingAsr
from lira.audio.kws import KwsStream, WakeWordKws
from lira.config import LlmConfig
from lira.dialog.intents import PLAYBACK_WHITELIST_KEYWORDS_FILE
from lira.dialog.route_dispatch import RouteDispatch
from lira.dialog.state_machine import AudioRoute, DialogEngine, State
from lira.dialog.router import Router
from lira.dialog.state_machine import DialogCallbacks
from lira.hal.mock import MockIrController
from lira.llm.client import LlmClient, make_remote_handler, make_text_polisher
from lira.privacy import PrivacyGatedSink, PrivacyState
from lira.sync import make_snapshot_announcer
from lira.vision.engine import OcrEngine, OcrLine
from lira.vision.reading import ReadingPipeline

WAKEWORD = "小丽拉"
CHUNK_FRAMES = 1600  # 0.1s @16k，与 MicDistributor 同粒度


# ---------- 音频注入源（mock mic） ----------

class WavQueueSource:
    """队列驱动的 mock 麦克风：say_wav 按序注入 wav，队列空即"环境安静"。

    read_chunk 永不 EOF（真实麦克风语义）； teardown 经 orchestrator 取消。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read_chunk(self, size: int) -> bytes:
        return await self._queue.get()

    def push_samples(self, samples: np.ndarray) -> None:
        """float32 波形按 0.1s 块入队。"""
        for start in range(0, len(samples), CHUNK_FRAMES):
            chunk = samples[start : start + CHUNK_FRAMES]
            self._queue.put_nowait((chunk * 32767.0).astype(np.int16).tobytes())

    def push_silence(self, seconds: float) -> None:
        self.push_samples(np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32))

    def push_wav(self, wav_path: Path) -> None:
        with wave.open(str(wav_path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        self.push_samples(samples)


# ---------- 朗读块播放器（录制 fake，计块+可暂停观测） ----------

class PacedBlockSpeaker:
    """BlockSpeaker 协议的录制实现：每块播放耗时 pace_seconds（可暂停观测）。

    pace 取值模拟真实语速的块间 runway，让"暂停"在朗读进行中注入可观测
    （AE4）；真实 TTS 全块播报更长，此处取保守短值控制测试时长。
    """

    def __init__(self, pace_seconds: float = 0.5) -> None:
        self.volume: float = 1.0
        self.pace_seconds = pace_seconds
        self.played: list[str] = []

    async def play(self, text: str) -> None:
        self.played.append(text)
        await asyncio.sleep(self.pace_seconds)


# ---------- Fake OCR / Fake Camera ----------

class FakeOcrEngine(OcrEngine):
    """固定文本 OCR：read() 返回构造时配置的文本行（拍摄内容不是被测对象）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.read_calls = 0

    def detect(self, image: np.ndarray) -> list:  # type: ignore[override]
        return []

    def recognize(self, crops) -> list[str]:  # type: ignore[override]
        return []

    def read(self, image: np.ndarray) -> list[OcrLine]:  # type: ignore[override]
        self.read_calls += 1
        lines: list[OcrLine] = []
        for i, ln in enumerate(self.text.splitlines()):
            if ln.strip():
                y = float(i * 40)
                lines.append(OcrLine(
                    polygon=((10.0, y), (500.0, y), (500.0, y + 36.0), (10.0, y + 36.0)),
                    text=ln.strip(),
                ))
        return lines


class FakeCamera:
    """单张白图 JPEG 的 mock 相机（_grab 只需可解码图像）。"""

    def __init__(self) -> None:
        image = np.full((240, 640, 3), 255, dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", image)
        assert ok
        self._jpeg = jpeg.tobytes()
        self.capture_calls = 0

    async def capture(self) -> bytes:
        self.capture_calls += 1
        return self._jpeg


# ---------- LLM stub（网络层计数 = AE1 断言面） ----------

def _sse_body(*deltas: str) -> bytes:
    lines: list[str] = []
    base = {"id": "e2e", "object": "chat.completion.chunk",
            "created": 1700000000, "model": "e2e-model"}
    for delta in deltas:
        chunk = {**base, "choices": [{"index": 0, "delta": {"content": delta},
                                      "finish_reason": None}]}
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    lines.append("data: " + json.dumps(final, ensure_ascii=False) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


#: 远程转白话的罐头回复（默认为长文：多朗读块给 AE4 暂停留 runway）
DEFAULT_LLM_REPLY = (
    "这个药主要是用来退烧和止痛的。大人一次吃一片，一天最多吃四次，"
    "两次之间要隔开六个小时。最好吃完饭以后再吃，这样胃会舒服一些。"
    "如果吃了三天还没有好转，就要去医院让医生看一看。"
    "千万不要一次吃很多，也不要把药分给别人吃。"
    "药品要放在小孩子拿不到的地方，避免误食。"
    "打开电视的时候请不要影响吃药休息。"
)


class FakeRemoteLlm:
    """httpx2.MockTransport handler：请求计数 + 断网开关（AE1/AE2 断言面）。"""

    SSE_HEADERS = {"content-type": "text/event-stream"}

    def __init__(self, reply: str = DEFAULT_LLM_REPLY) -> None:
        self.reply = reply
        self.requests = 0
        self.prompts: list[str] = []
        self.net_ok = True

    def __call__(self, request):  # noqa: ANN001 - httpx2 handler 签名
        import httpx2 as httpx

        self.requests += 1
        self.prompts.append(json.loads(request.read().decode("utf-8"))["messages"][-1]["content"])
        if not self.net_ok:
            raise httpx.ConnectError("network down (test)", request=request)
        return httpx.Response(200, headers=self.SSE_HEADERS,
                              content=_sse_body(*self.reply))


def make_e2e_llm(fake: FakeRemoteLlm) -> LlmClient:
    import httpx2 as httpx
    from openai import AsyncOpenAI

    cfg = LlmConfig(
        base_url="http://e2e.test/v1", api_key="sk-e2e", model="e2e-model",
        timeout_seconds=5.0, breaker_failure_threshold=3,
        breaker_cooldown_seconds=60.0, wait_feedback_seconds=2.0,
        colloquial_threshold_chars=150,
    )
    oai = AsyncOpenAI(
        base_url=cfg.base_url, api_key=cfg.api_key,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        max_retries=0,
    )
    return LlmClient(cfg, openai_client=oai)




# ---------- 状态机回调装配（DialogCallbacks 实现） ----------

class _Callbacks:
    """同步回调面：speak 记录 + 播放控制/拍摄/红外委托（与 main.py 装配一致）。"""

    def __init__(self, h: "DeviceHarness") -> None:
        self._h = h

    # speak：记录断言面（真实设备为 TTS 播报；e2e 记文本 + 可选留档）
    def speak(self, text: str) -> None:
        self._h.speak_log.append(text)

    def start_listening(self) -> None:
        if self._h.asr_stream is not None:
            self._h.asr_stream.reset()

    def set_audio_route(self, route: AudioRoute) -> None:
        self._h.route = route

    def start_capture(self) -> None:
        asyncio.ensure_future(self._h.pipeline.run_capture())

    def send_ir(self, device: str, action: str) -> None:
        # 状态机已完成高危确认 → 装配层以 confirmed=True 过第二道闸
        # （第二道闸仍独立校验 enabled/动作/码值，防御直调）
        asyncio.ensure_future(self._h._send_ir(device, action))

    def playback_pause(self) -> None:
        self._h.pipeline.playback_pause()

    def playback_resume(self) -> None:
        self._h.pipeline.playback_resume()

    def playback_stop(self) -> None:
        self._h.pipeline.playback_stop()

    def playback_read_again(self) -> None:
        self._h.pipeline.playback_read_again()

    def playback_volume_up(self) -> None:
        self._h.pipeline.playback_volume_up()

    def playback_volume_down(self) -> None:
        self._h.pipeline.playback_volume_down()


# ---------- 整机 harness ----------

class DeviceHarness:
    """全 mock 整机：音频注入 → 隐私门 → 路由 → 状态机 → IR/阅读/同步。

    Args:
        voices: (WakeWordKws, StreamingAsr, WakeWordKws[白名单]) 或 None。
            None = 无模型环境（文本注入 say_text 仍可驱动状态机）。
        ocr_text: FakeOcrEngine 固定识别文本（每次拍摄覆盖）。
    """

    def __init__(
        self,
        db_path: Path,
        voices: tuple[WakeWordKws, StreamingAsr, WakeWordKws] | None,
        ocr_text: str = "",
    ) -> None:
        self.store = ApplianceStore(db_path)
        self.privacy = PrivacyState()
        self.ir = MockIrController()
        self.ir_service = IRService(self.store, self.ir)
        self.speak_log: list[str] = []
        self.ir_sent: list[tuple[str, str]] = []
        self.ir_errors: list[tuple[str, str, str]] = []
        self.route = AudioRoute.WAKE
        self.clock = 0.0

        # LLM stub + 白话钩子
        self.llm_fake = FakeRemoteLlm("这是转成大白话的说明书内容。")
        self.llm = make_e2e_llm(self.llm_fake)
        self.ocr = FakeOcrEngine(ocr_text)
        self.camera = FakeCamera()
        self.speaker = PacedBlockSpeaker()
        self.pipeline = ReadingPipeline(
            self.camera, self.ocr, self.speaker,
            on_capture_done=lambda ok: asyncio.ensure_future(self.engine.on_capture_done(ok)),
            on_reading_finished=lambda: asyncio.ensure_future(self.engine.on_reading_finished()),
            text_polisher=make_text_polisher(self.llm),
        )

        # 识别流（无模型环境为 None：文本注入路径仍可全面驱动状态机）
        self.wake_stream: KwsStream | None = None
        self.asr_stream: AsrStream | None = None
        self.playback_stream: KwsStream | None = None
        if voices is not None:
            wake_kws, asr_engine, playback_kws = voices
            self.wake_stream = wake_kws.create_stream()
            self.asr_stream = asr_engine.create_stream()
            self.playback_stream = playback_kws.create_stream()

        # 状态机 + 路由（家电表每次匹配现读本地库 → 同步更新即时生效）
        self.callbacks = _Callbacks(self)
        self.engine = DialogEngine(
            Router(appliances=lambda: self._store_appliances(),
                   remote_llm=make_remote_handler(self.llm),
                   remote_available=self.llm.is_available),
            self.callbacks,
            wake_allowed=lambda: not self.privacy.is_on,
        )
        self.source = WavQueueSource()
        self._gate = PrivacyGatedSink(
            RouteDispatch(streams=self, wakeword=WAKEWORD, spawn=asyncio.ensure_future),
            self.privacy,
        )
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

        # 隐私通道：后果播报
        from lira.privacy import attach_announcer

        attach_announcer(self.privacy, self.callbacks.speak)

    # ---------- 装配辅助 ----------

    def _spawn(self, coro) -> asyncio.Task:
        """跟踪状态机在途任务（drain 的收敛依据）。"""
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    def _store_appliances(self) -> tuple:
        from lira.appliances.models import ApplianceModel

        return tuple(a.to_dialog() for a in self.store.get_all_appliances())

    async def _send_ir(self, device: str, action: str) -> None:
        try:
            await self.ir_service.send_ir(device, action, confirmed=True)
            self.ir_sent.append((device, action))
        except ApplianceError as exc:
            self.ir_errors.append((device, action, exc.kind))

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.store.close()

    async def _run(self) -> None:
        """编排主循环（= main.py 未来主循环）：注入音频 → 隐私门 → 路由 → tick。"""
        while True:
            chunk = await self.source.read_chunk(CHUNK_FRAMES * 2)
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            self._gate.feed(samples)  # 隐私 ON 时样本在此被丢弃（结构性关麦）
            self.clock += len(samples) / SAMPLE_RATE  # 虚拟时钟随音频推进
            self.engine.tick(self.clock)

    # ---------- 测试驱动 API ----------

    async def say_wav(self, name: str, *, tail_seconds: float = 1.2) -> None:
        """注入一条语音（wav 夹具 + 静音尾巴）并**等待处理收敛**：
        音频队列清空 + 状态机在途任务完成——保证下一条注入时路由/状态正确。

        tail_seconds: 静音尾巴时长。LISTEN 路由的流式 ASR 切句需要
        ≥2.0s（rule1_min_trailing_silence）尾静音，经 ASR 解码的词条
        （confirm/volume_up 等）须传 2.4；KWS 命中（wake/白名单词）
        1.2s 足够（KWS 自带 ~0.8s 内部端点）。
        """
        wav = Path(__file__).parent / "audio_fixtures" / f"{name}.wav"
        self.source.push_wav(wav)
        self.source.push_silence(tail_seconds)
        await self.drain()

    async def say_text(self, text: str) -> None:
        """文本注入（ASR 降级通道）：直接驱动状态机 ASR 入口并等待完成。

        仅用于真实 ASR 对该短语不可靠的场景（记录为偏差）；KWS 验证
        （AE1/AE4）不走此通道。
        """
        await self.engine.on_asr_text(text)

    def advance(self, seconds: float) -> None:
        """快进虚拟时钟（以静音填充，保持音频推进与时钟一致）。"""
        self.source.push_silence(seconds)

    async def drain(self, timeout: float = 30.0) -> None:
        """等待注入音频全部被编排器消费 + 状态机在途任务收敛。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.source._queue.qsize() == 0:
                await asyncio.sleep(0.05)
                if self.source._queue.qsize() == 0:
                    break
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("音频队列未在时限内消费完")
        # 让 ensure_future 出的状态机/拍摄任务跑完（READING 会话任务除外）
        deadline = time.monotonic() + timeout
        while self._pending and time.monotonic() < deadline:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        await asyncio.sleep(0)

    async def wait_state(self, *states: State, timeout: float = 5.0) -> State:
        """等待状态机进入任一目标态。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.engine.state in states:
                return self.engine.state
            await asyncio.sleep(0.01)
        raise TimeoutError(
            f"等待状态 {states} 超时，当前 {self.engine.state}；speak_log={self.speak_log}"
        )

    async def wait_speech(self, pred, timeout: float = 5.0) -> str:
        """等待 speak_log 出现满足谓词的播报，返回该条文本。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for text in self.speak_log:
                if pred(text):
                    return text
            await asyncio.sleep(0.01)
        raise TimeoutError(f"等待播报超时；speak_log={self.speak_log}")

    async def settle(self, seconds: float = 0.3) -> None:
        """等待在途任务（ensure_future 的状态机/拍摄协程）收敛。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        for _ in range(3):
            await asyncio.sleep(0)

    # ---------- 同步接入（AE5/AE7/对抗用例与真实后台联通用） ----------

    def snapshot_announce_hook(self):
        """SyncClient(on_snapshot_applied=...) 钩子：应用后播报"新禁用名单"
        （AE7 话术），幂等快照不重复播报（只在 enabled→disabled 变化时）。"""

        return make_snapshot_announcer(self.store, self.speak_log.append)
