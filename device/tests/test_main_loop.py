"""M7 主循环接线测试（lira/main.py DeviceRuntime 装配层）。

策略：组件全部注入替身（音频源/语音栈/TTS/OCR/LLM），验证生产装配（与 e2e
DeviceHarness 同构）在替身驱动下的行为与生命周期：
  - 音频源 EOF -> 主循环干净退出；
  - 唤醒 -> 指令 -> TTS 播报 + mock IR 记录（生产回调接线正确）；
  - 路由分发喂入唤醒词命中 -> on_wake 驱动状态机；
  - 同步监督循环断线重连（transport 工厂注入）；
  - 快照播报钩子与 UI uvicorn in-process 挂载。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import numpy as np
import pytest

from lira.hal.base import AudioIO
from lira.hal.mock import MockDisplay, MockIrController
from lira.audio._paths import SAMPLE_RATE
from lira.config import load_config
from lira.main import AudioStack, DeviceRuntime, build_board_hal
from lira.sync import SyncError, make_snapshot_announcer

# ---------- 替身 ----------


class FakeStream:
    """KWS/ASR 流替身：可选命中队列，无模型依赖。"""

    def __init__(self, hits: list[str] | None = None) -> None:
        self.hits = list(hits or [])

    def feed(self, samples) -> None:
        pass

    def feed_and_poll(self, samples) -> str | None:
        if self.hits:
            return self.hits.pop(0)
        return None

    def text(self) -> str:
        return ""

    def is_endpoint(self) -> bool:
        return False

    def reset(self) -> None:
        pass


class FakeKws:
    """WakeWordKws 替身：create_stream 出 FakeStream（可配命中队列）。"""

    def __init__(self, hits: list[str] | None = None) -> None:
        self._hits = list(hits or [])

    def create_stream(self) -> FakeStream:
        return FakeStream(self._hits)


class FakeAsr:
    def create_stream(self) -> FakeStream:
        return FakeStream()


class FakeTts:
    """TtsEngine 替身：记录 speak 调用，事件立即 set（无音频输出）。"""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, float, float]] = []
        self.chimes = 0
        self.cancelled_owners = []
        self.closed = False

    def speak(self, text: str, speed: float = 1.0, volume: float = 1.0, *, owner=None) -> asyncio.Event:
        self.spoken.append((text, speed, volume))
        done = asyncio.Event()
        done.set()
        return done

    def chime(self, *, owner=None, volume=1):
        self.chimes += 1
        done = asyncio.Event()
        done.set()
        return done

    def cancel_owner(self, owner):
        self.cancelled_owners.append(owner)

    async def stop(self, owner=None):
        self.cancel_owner(owner)

    async def close(self):
        self.closed = True


class FakeLlm:
    """LlmClient 替身：远程恒不可用（路由走本地规则），白话钩子恒本地直读。"""

    #: LIRA_PERSONAL_MEMORY=1 装配分支读 llm._client（PersonalMemory 的 SDK 引用）
    _client = None

    def is_available(self) -> bool:
        return False

    def needs_colloquial(self, text: str) -> bool:
        return False

    async def colloquial(self, text: str) -> str:
        return text


class FakeOcr:
    """OcrEngine 替身：固定文本行（拍摄内容不是被测对象）。"""

    def __init__(self, text: str = "第一行测试文本") -> None:
        self.text = text
        self.read_calls = 0

    def detect(self, image):
        return []

    def recognize(self, crops):
        return []

    def read(self, image):
        self.read_calls += 1
        lines = []
        for i, ln in enumerate(self.text.splitlines()):
            y = float(i * 40)
            lines.append(
                _line((10.0, y), (500.0, y), (500.0, y + 36.0), (10.0, y + 36.0), ln)
            )
        return lines


def _line(p1, p2, p3, p4, text: str):
    from lira.vision.engine import OcrLine

    return OcrLine(polygon=(p1, p2, p3, p4), text=text)


class FakeCamera(AudioIO):  # 仅复用 async 上下文骨架；相机语义见 capture
    def __init__(self) -> None:
        self.capture_calls = 0

    async def capture(self) -> bytes:
        import cv2

        import numpy as _np

        image = _np.full((240, 640, 3), 255, dtype=_np.uint8)
        ok, jpeg = cv2.imencode(".jpg", image)
        assert ok
        self.capture_calls += 1
        return jpeg.tobytes()

    async def read_chunk(self, size: int) -> bytes:
        raise NotImplementedError

    async def play(self, data: bytes) -> None:
        raise NotImplementedError


class SilenceSource(AudioIO):
    """无限静音源（测试）：队列驱动，close() 投递 EOF 结束主循环。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read_chunk(self, size: int) -> bytes:
        return await self._queue.get()

    async def play(self, data: bytes) -> None:
        raise NotImplementedError

    def push_silence(self, seconds: float) -> None:
        """按 0.1s 块投递静音（驱动编排循环与 tick）。"""
        samples = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)
        for start in range(0, len(samples), 1600):
            self._queue.put_nowait(
                (samples[start : start + 1600] * 32767.0).astype(np.int16).tobytes()
            )

    def push_eof(self) -> None:
        """投递 EOF 哨兵（不覆盖 HAL 的 async close）。"""
        self._queue.put_nowait(b"")


# ---------- 夹具 ----------


def make_cfg(tmp_path, *, ws_url: str = "", ui_port: int = 0):
    yaml_text = (
        "llm:\n"
        "  api_key: sk-test\n"
        "hal:\n"
        "  backend: mock\n"
        f"db_path: {(tmp_path / 'device.db').as_posix()}\n"
        "ui:\n"
        "  host: 127.0.0.1\n"
        f"  port: {ui_port}\n"
    )
    if ws_url:
        yaml_text += f"sync:\n  ws_url: {ws_url}\n  device_token: tok-1\n"
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_config(path, env={})


@dataclass
class Rig:
    """测试台架：runtime + 可控源 + 断言面（engine 请走 rig.runtime.engine）。"""

    runtime: DeviceRuntime
    source: SilenceSource
    tts: FakeTts
    ir: MockIrController


def make_rig(tmp_path, *, ws_url: str = "", transport_factory=None, ui_port: int = 0,
             clock=None, ir=None, settings=None):
    """整机替身装配（mock HAL + 假语音栈 + 假 LLM/OCR）。

    ir 可注入真实 HAL 资源（如会 open 失败的 BroadlinkIrController，F1 回归用），
    默认 MockIrController。settings 可注入带持久化钩子的 DeviceSettings（U8）。
    """
    from lira.appliances.models import ApplianceModel
    from lira.appliances.store import ApplianceStore
    from lira.settings import DeviceSettings

    cfg = make_cfg(tmp_path, ws_url=ws_url, ui_port=ui_port)
    store = ApplianceStore(tmp_path / "device.db")
    store.upsert_appliance(
        ApplianceModel(
            name="台灯",
            aliases=("台灯",),
            actions={"打开": ("打开", "开")},
            codes={"打开": "CODE_LAMP_ON"},
        )
    )
    tts = FakeTts()
    ir = MockIrController() if ir is None else ir
    source = SilenceSource()
    hal = {"camera": FakeCamera(), "audio": source, "ir": ir, "display": MockDisplay()}
    settings = settings or DeviceSettings()
    kwargs = {}
    if transport_factory is not None:
        kwargs["sync_transport_factory"] = transport_factory
    if clock is not None:
        kwargs["clock"] = clock
    runtime = DeviceRuntime(
        cfg=cfg,
        hal=hal,
        store=store,
        settings=settings,
        audio=AudioStack(wake_kws=FakeKws(), asr=FakeAsr(), playback_kws=FakeKws(),
                         command_kws=FakeKws(), tts=tts),
        ocr=FakeOcr(),
        llm=FakeLlm(),
        **kwargs,
    )
    rig = Rig(runtime=runtime, source=source, tts=tts, ir=ir)
    return rig

# ---------- 断言辅助 ----------


async def settle(rt, timeout: float = 5.0) -> None:
    """等待 runtime 在途任务收敛（轮询 _pending）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while rt._pending:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("在途任务未收敛")
        await asyncio.gather(*list(rt._pending), return_exceptions=True)
        await asyncio.sleep(0.01)


class TestLifecycle:
    async def test_eof_shuts_down_cleanly(self, tmp_path):
        """音频源 EOF -> 主循环退出，在途任务与 HAL 全部收敛。"""
        rig = make_rig(tmp_path)
        task = asyncio.ensure_future(rig.runtime.run())
        await asyncio.sleep(0.05)
        rig.source.push_eof()
        await asyncio.wait_for(task, timeout=5.0)
        assert task.done() and not task.exception()
        assert rig.tts.closed


class TestOwnedEffects:
    async def test_wake_only_preroll_endpoint_keeps_original_eight_second_deadline(self, tmp_path):
        rig = make_rig(tmp_path, clock=lambda: 1000.0)
        rt = rig.runtime
        rt.wake_stream.hits.append("小鹿")
        rt._gate.feed(np.full(1600, .1, dtype=np.float32))
        await settle(rt)
        assert rt.engine.state.value == "listening"
        assert rt.engine._awaiting_endpoint
        rt.asr_stream.text = lambda: "小鹿"
        rt.asr_stream.is_endpoint = lambda: True
        rt._gate.feed(np.zeros(12000, dtype=np.float32))
        await settle(rt)
        rt.engine.tick(1008.0)
        assert rt.engine.state.value == "standby"

    async def test_queued_wake_before_privacy_cycle_is_discarded(self, tmp_path):
        rig = make_rig(tmp_path)
        rt = rig.runtime
        rt.wake_stream.hits.append("小鹿")
        rt._gate.feed(np.zeros(1600, dtype=np.float32))
        await rt.privacy.set_enabled(True, source="test")
        await rt.privacy.set_enabled(False, source="test")
        await settle(rt)
        assert rt.engine.state.value == "standby"
        assert rig.tts.chimes == 0

    @pytest.mark.parametrize("interrupt", ["cancel", "wake", "privacy"])
    async def test_queued_ir_is_rejected_if_owner_is_invalidated(self, tmp_path, interrupt):
        rig = make_rig(tmp_path)
        rt = rig.runtime
        await rt.engine.on_wake()
        await rt.engine.on_asr_text("打开台灯")
        # IR task is queued but has not run yet; interrupt without yielding.
        if interrupt == "cancel":
            await rt.engine.on_asr_text("取消")
        elif interrupt == "wake":
            await rt.engine.on_wake()
        else:
            await rt.privacy.set_enabled(True, source="test")
        await settle(rt)
        assert rig.ir.sent_codes == []

    async def test_high_risk_admission_needs_same_owner_confirmation(self, tmp_path):
        rig = make_rig(tmp_path)
        rt = rig.runtime
        await rt.engine.on_wake()
        owner = rt.engine.owner
        assert not rt.engine.admit_ir(owner, "取暖器", "打开", confirmed=True)
        assert not rt.engine.admit_ir(None, "台灯", "打开", confirmed=False)
        await rt.engine.on_asr_text("结束聊天")
        assert not rt.engine.admit_ir(owner, "台灯", "打开", confirmed=False)

    async def test_privacy_invalidates_pending_answer_and_stops_owned_audio(self, tmp_path):
        rig = make_rig(tmp_path)
        rt = rig.runtime
        await rt.engine.on_wake()
        owner = rt.engine.owner
        await rt.privacy.set_enabled(True, source="test")
        assert rt.engine.state.value == "standby"
        assert not rt.engine.is_current(owner)
        assert owner in rig.tts.cancelled_owners
        assert rt.dispatcher.generation != rt.engine.listening_generation


class TestDialogWiring:
    async def test_book_command_reads_original_without_llm(self, tmp_path):
        from lira.dialog import phrasebook as pb

        rig = make_rig(tmp_path)
        rt = rig.runtime
        original = "这是需要逐字核对的书本内容。" * 20
        rt.ocr.text = original

        async def forbidden_polish(text):
            pytest.fail("读书指令不得改写原文")

        rt.pipeline._text_polisher = forbidden_polish
        await rt.engine.on_wake()
        await rt.engine.on_asr_text("帮我读下这本书")
        await settle(rt)
        spoken = [text for text, _, _ in rig.tts.spoken]
        assert rig.tts.chimes == 1
        assert spoken[:3] == [pb.WAKE_ACK, pb.CAPTURE_GUIDANCE, pb.OCR_COMPLETE]
        assert "".join(spoken[3:]) == original
        assert rt.camera.capture_calls == 1
        assert rt.engine.state.value == "standby"

    async def test_prompt_playback_and_settle_suppress_asr_then_recover(self, tmp_path):
        # 自说自话修复：提示语播放期间（AEC 残余）不得进识别流；
        # 释放沿后的静默窗吞弃延迟尾音，窗口过后恢复正常接收。
        rig = make_rig(tmp_path)
        rt = rig.runtime
        await rt.engine.on_wake()
        rt.tts.occupied = True

        received = []
        rt.asr_stream.feed = lambda samples: received.append(len(samples))
        rt._gate.feed(np.zeros(1600, dtype=np.float32))
        assert received == []
        rt.tts.occupied = False
        rt._gate.feed(np.zeros(11200, dtype=np.float32))
        assert received == []
        rt._gate.feed(np.zeros(1600, dtype=np.float32))
        assert received == [1600]
        rt.engine.tick(rt.engine.now + 8)
        assert rt.engine.state.value == "standby"

    async def test_book_retry_preserves_original_reading_mode(self, tmp_path):
        rig = make_rig(tmp_path)
        rt = rig.runtime
        read_calls = 0
        read = rt.ocr.read

        def read_after_retry(image):
            nonlocal read_calls
            read_calls += 1
            return [] if read_calls == 1 else read(image)

        async def forbidden_polish(text):
            pytest.fail("读书重拍后仍不得改写原文")

        rt.ocr.read = read_after_retry
        rt.pipeline._text_polisher = forbidden_polish
        await rt.engine.on_wake()
        await rt.engine.on_asr_text("帮我读下这本书")
        await settle(rt)
        assert rt.camera.capture_calls == 2
        assert read_calls == 2
        assert rt.engine.state.value == "standby"
        assert not rt.engine.read_original

    async def test_wake_then_appliance_command(self, tmp_path):
        """唤醒 -> 指令 -> TTS 播报 + mock IR 记录（生产回调接线正确）。"""
        rig = make_rig(tmp_path)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.05)
            await rig.runtime.engine.on_wake()
            assert rig.runtime.engine.state.value == "listening"
            await rig.runtime.engine.on_asr_text("打开台灯")
            await settle(rig.runtime)
            spoken = [t for t, _, _ in rig.tts.spoken]
            assert rig.tts.chimes == 1
            assert any("台灯" in t for t in spoken)
            assert rig.ir.sent_codes == ["CODE_LAMP_ON"]
            assert rig.runtime.engine.state.value == "standby"
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_route_dispatch_wake_hit_drives_engine(self, tmp_path):
        """WAKE 路由喂入命中唤醒词的流 -> 分发器驱动 on_wake。"""
        rig = make_rig(tmp_path)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.05)
            rig.runtime.wake_stream.hits.append("小鹿 @xiao lu")
            samples = np.zeros(1600, dtype=np.float32)
            rig.runtime._gate.feed(samples)
            await asyncio.sleep(0.1)
            assert rig.runtime.engine.state.value == "listening"
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_capture_wiring_returns_task_and_reads(self, tmp_path):
        """读一下 -> CAPTURING；start_capture 返回任务句柄且拍摄真实发生。"""
        rig = make_rig(tmp_path)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.05)
            await rig.runtime.engine.on_wake()
            await rig.runtime.engine.on_asr_text("读一下")
            await asyncio.sleep(0.3)
            assert rig.runtime.engine.state.value in ("capturing", "reading", "standby")
            camera = rig.runtime._hal["camera"]
            assert camera.capture_calls >= 1
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)


class FakeTtsSettings:
    """DeviceSettings 同形替身（同步注入）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def set_volume(self, value: float) -> None:
        self.calls.append(("volume", value))

    def set_tts_speed(self, value: float) -> None:
        self.calls.append(("speed", value))


class ScriptedTransport:
    """SyncTransport 替身：connect 可失败，auth 后阻塞直至测试放行。"""

    def __init__(self, fail_connect: bool = False) -> None:
        self.fail_connect = fail_connect
        self.closed = False
        self._auth_sent = False
        self._release = asyncio.Event()

    async def send(self, frame) -> None:
        if self.fail_connect:
            raise SyncError("connect failed (scripted)")

    async def receive(self):
        if self.fail_connect:
            raise SyncError("connect failed (scripted)")
        if self._auth_sent:
            await self._release.wait()
            raise SyncError("released by test")
        self._auth_sent = True
        return {"type": "auth_ok"}

    async def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self._release.set()


class TestSyncSupervisor:
    async def test_reconnects_after_failure(self, tmp_path, monkeypatch):
        """首连失败 -> 退避 -> 重连成功（成功会话后恢复会话期退避重置）。"""
        import lira.main as main_mod

        monkeypatch.setattr(main_mod, "SYNC_RECONNECT_MIN_SECONDS", 0.05)
        monkeypatch.setattr(main_mod, "SYNC_RECONNECT_MAX_SECONDS", 0.1)
        made: list[ScriptedTransport] = []

        def factory(url: str) -> ScriptedTransport:
            t = ScriptedTransport(fail_connect=len(made) == 0)
            made.append(t)
            return t

        rig = make_rig(tmp_path, ws_url="ws://backend.test/ws", transport_factory=factory)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.6)
            assert len(made) >= 2, f"应已重连: {len(made)}"
            assert made[0].closed, "失败 transport 应被关闭"
            assert not made[-1].closed, "当前会话保持连接"
        finally:
            made[-1].release()
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)
            assert made[-1].closed, "停机后当前会话也应关闭"


class TestAssemblyHelpers:
    def test_snapshot_announcer_speaks_newly_disabled(self, tmp_path):
        """AE7 钩子：对比 prev/now enabled -> 只播报本次新禁用的设备。"""
        from lira.appliances.models import ApplianceModel
        from lira.appliances.store import ApplianceStore

        store = ApplianceStore(tmp_path / "device.db")
        store.upsert_appliance(
            ApplianceModel(name="取暖器", aliases=("取暖器",),
                           actions={"打开": ("打开",)}, codes={"打开": "C1"})
        )
        spoken: list[str] = []
        announce = make_snapshot_announcer(store, spoken.append)
        store.upsert_appliance(
            ApplianceModel(name="取暖器", aliases=("取暖器",),
                           actions={"打开": ("打开",)}, codes={"打开": "C1"}, enabled=False)
        )
        announce(None, {"取暖器": True})
        assert len(spoken) == 1 and "取暖器" in spoken[0]
        announce(None, {"取暖器": False})  # 已是禁用态，不重复播报
        assert len(spoken) == 1
        store.close()

    def test_build_board_hal_constructs_and_warns(self, tmp_path, caplog, monkeypatch):
        monkeypatch.setattr("lira.audio.devices.board_audio_device",
                            lambda value, **kwargs: int(value))
        """board 后端装配不触硬件：IR 未配置回退 Mock 并告警；配置即换 BroadlinkIrController。"""
        from lira.hal.board import V4l2IspCamera
        from lira.hal.board.ir_broadlink import BroadlinkIrController
        from lira.hal.mock.ir import MockIrController
        from lira.audio.mic import SoundDeviceMic

        base = (
            "llm:\n  api_key: k\n"
            f"db_path: {(tmp_path / 'device.db').as_posix()}\n"
            "hal:\n  backend: board\n  mic_device: '3'\n  speaker_device: '2'\n"
        )
        path = tmp_path / "cfg.yaml"
        path.write_text(base, encoding="utf-8")
        cfg = load_config(path, env={})
        with caplog.at_level(logging.WARNING):
            hal = build_board_hal(cfg)
        assert isinstance(hal["camera"], V4l2IspCamera)
        assert isinstance(hal["audio"], SoundDeviceMic)
        assert isinstance(hal["ir"], MockIrController)  # 未配置回退 Mock
        assert any("IR 未配置" in r.message for r in caplog.records)

        path.write_text(base + "  ir_host: 192.168.101.27\n  ir_mac: ec:0b:ae:0c:74:e4\n",
                        encoding="utf-8")
        cfg = load_config(path, env={})
        hal = build_board_hal(cfg)  # 构造不触网（open 时才连）
        assert isinstance(hal["ir"], BroadlinkIrController)

    async def test_enter_hal_resources_ir_failure_degrades_to_mock(self, caplog, tmp_path):
        """R9 韧性：带 fallback_factory 的资源 open 失败（离线/锁定）降级 Mock，主循环不崩。"""
        from contextlib import AsyncExitStack

        from lira.appliances import IRService
        from lira.appliances.store import ApplianceStore
        from lira.hal.base import HalError
        from lira.hal.mock.ir import MockIrController
        from lira.main import enter_hal_resources

        class FailingIr:
            fallback_factory = MockIrController  # BroadlinkIrController 同款声明

            async def __aenter__(self):
                raise HalError("BroadLink 认证被拒")

            async def __aexit__(self, *exc):
                return False

        hal = {"camera": MockIrController(), "ir": FailingIr()}  # camera 槽位用无害 mock 占位
        stack = AsyncExitStack()
        with caplog.at_level(logging.ERROR):
            await enter_hal_resources(hal, stack)
        assert isinstance(hal["ir"], MockIrController)
        assert any("ir 初始化失败" in r.message for r in caplog.records)
        # 消费面（IRService 实际拿到 Mock）由
        # test_ir_degrade_reaches_ir_service_via_runtime_run 钉住——在此手工构造
        # IRService 只能证明构造器存参，对 F1 接线缺陷无回归力（testing-r2）
        await stack.aclose()

    async def test_ir_degrade_reaches_ir_service_via_runtime_run(self, tmp_path):
        """F1 回归（mutation 防御）：IR open 失败降级 Mock 必须到达 IRService——
        用真实 BroadlinkIrController（factory 必超时）走完整 run() 装配路径，
        断言 run() 在 enter_hal_resources 之后绑定的是降级 Mock。"""
        from lira.hal.board.ir_broadlink import BroadlinkIrController
        from lira.hal.mock.ir import MockIrController

        async def timeout_factory(host, timeout):
            raise type("NetworkTimeoutError", (Exception,), {})(-4000, "timeout")

        ir = BroadlinkIrController("192.168.101.27", "ec:0b:ae:0c:74:e4",
                                   device_factory=timeout_factory)
        rig = make_rig(tmp_path, ir=ir)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            # 轮询等待 run() 完成 HAL 进入与 IR 服务装配（构造期该属性不存在）
            service = None
            for _ in range(200):
                service = getattr(rig.runtime, "ir_service", None)
                if service is not None:
                    break
                await asyncio.sleep(0.02)
            assert service is not None, "run() 未在 HAL 进入后装配 ir_service"
            assert isinstance(service._ir, MockIrController)
            assert service._ir is not ir, "IRService 仍绑定降级前的 Broadlink 控制器"
            assert rig.runtime.learning is not None
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_enter_hal_resources_non_ir_failure_raises(self):
        """无 fallback_factory 的资源失败仍快速上抛（致命可见，不吞）。"""
        from contextlib import AsyncExitStack

        from lira.hal.base import HalError
        from lira.main import enter_hal_resources

        class FailingCam:
            async def __aenter__(self):
                raise HalError("相机故障")

            async def __aexit__(self, *exc):
                return False

        with pytest.raises(HalError):
            await enter_hal_resources({"camera": FailingCam()}, AsyncExitStack())

    async def test_main_exit_codes(self, tmp_path, monkeypatch):
        """--dry-run=0；非 dry-run 缺 api_key=2（真实装配错误路径）。"""
        from lira import main as main_mod

        # 缺 api_key（非 dry-run，配置文件不存在 -> 默认值）-> 2
        monkeypatch.setenv("LIRA_CONFIG", str(tmp_path / "nope.yaml"))
        assert main_mod.main([]) == 2
        # dry-run（api_key 豁免）-> 0；装配图打到 stdout
        assert main_mod.main(["--dry-run"]) == 0

    async def test_ui_mount_serves_status_page(self, tmp_path):
        """UI in-process 挂载：uvicorn 端口可达，状态卡 200。"""
        import httpx

        rig = make_rig(tmp_path, ui_port=0)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.3)
            server = rig.runtime._ui_server
            assert server is not None and server.started
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:{port}/")
            assert r.status_code == 200 and "隐私" in r.text
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)


class TestReviewFixes:
    async def test_sync_supervisor_uses_real_transport_open(self, tmp_path):
        """评审 P0 回归：_run_sync 必须先 transport.open() 再 hello——
        用真实 WsSyncTransport 对本地 websockets 服务联跑（替身掩盖过此缺陷）。"""
        import json

        pytest.importorskip("websockets")
        from websockets.asyncio.server import serve

        hellos: list[dict] = []
        got_reply = asyncio.Event()

        async def handler(ws):
            frame = json.loads(await ws.recv())
            hellos.append(frame)
            await ws.send(json.dumps({"type": "auth_ok"}))
            await got_reply.wait()  # 保持连接直至用例收尾

        server = await serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        rig = make_rig(tmp_path, ws_url=f"ws://127.0.0.1:{port}")
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            for _ in range(100):
                if hellos:
                    break
                await asyncio.sleep(0.05)
            assert hellos and hellos[0]["type"] == "hello" and hellos[0]["token"] == "tok-1"
        finally:
            got_reply.set()
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)
            server.close()
            await server.wait_closed()

    async def test_engine_tick_wiring_via_injected_clock(self, tmp_path):
        """评审：engine.tick 接线此前无覆盖（删掉 tick 调用测试照绿）。
        注入假时钟快进 LISTENING 8s 窗口 -> 安静回待机。"""
        now = {"t": 1000.0}

        def fake_clock() -> float:
            return now["t"]

        rig = make_rig(tmp_path, clock=fake_clock)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.05)
            await rig.runtime.engine.on_wake()
            assert rig.runtime.engine.state.value == "listening"
            now["t"] += 9.0  # 第一个 8s 窗口超时
            # No microphone block arrives: the independent clock must expire.
            await asyncio.sleep(0.15)
            await settle(rig.runtime)
            assert rig.runtime.engine.state.value == "standby"
            assert [text for text, _, _ in rig.tts.spoken] == ["我在。"]
            assert rig.tts.chimes == 1
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)


# ---------- 常驻命令 KWS 通道整机接线（2026-10-06 计划 U2） ----------


class TestCommandChannel:
    async def test_command_hit_in_listening_ends_session(self, tmp_path):
        """LISTENING 中命令命中喊停词 → 会话立即回待机（生产接线全链）。"""
        rig = make_rig(tmp_path, clock=lambda: 1000.0)
        rt = rig.runtime
        rt.wake_stream.hits.append("小鹿")
        rt._gate.feed(np.full(1600, .1, dtype=np.float32))
        await settle(rt)
        assert rt.engine.state.value == "listening"
        rt.command_stream.hits.append("别念了")
        rt._gate.feed(np.zeros(1600, dtype=np.float32))
        await settle(rt)
        assert rt.engine.state.value == "standby"

    async def test_command_hit_in_standby_is_dropped(self, tmp_path):
        """STANDBY 命中丢弃：状态不变、无投递副作用。"""
        rig = make_rig(tmp_path, clock=lambda: 1000.0)
        rt = rig.runtime
        rt.command_stream.hits.append("别念了")
        rt._gate.feed(np.zeros(1600, dtype=np.float32))
        await settle(rt)
        assert rt.engine.state.value == "standby"
        assert rt.tts.spoken == []


class TestPoweroffWiring:
    async def test_poweroff_callback_spawns_system_poweroff(self, tmp_path, monkeypatch):
        """R4：poweroff 回调走系统级 systemctl poweroff（Restart=always 约束）。"""
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)

            class Proc:
                async def wait(self):
                    return 0

            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        rig = make_rig(tmp_path)
        rt = rig.runtime
        rt.callbacks.poweroff()
        await settle(rt)
        assert calls == [("systemctl", "poweroff")]

    async def test_poweroff_failure_does_not_crash(self, tmp_path, monkeypatch):
        """poweroff 子进程失败仅记日志（设备留在待机，主循环存活）。"""
        async def boom(*args, **kwargs):
            raise OSError("no systemd")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        rig = make_rig(tmp_path)
        rt = rig.runtime
        rt.callbacks.poweroff()
        await settle(rt)
        assert rt.engine.state.value == "standby"


# ---------- 勿扰装配层门（2026-10-06 计划 U5） ----------


class TestDndAssemblyGate:
    async def test_wake_in_dnd_suppresses_chime_and_ack(self, tmp_path):
        """R8：勿扰中裸唤醒词无提示音、无应答（播报/铃音门在装配层）。"""
        rig = make_rig(tmp_path, clock=lambda: 1000.0)
        rt = rig.runtime
        rt.engine.dnd = True
        rt.wake_stream.hits.append("小鹿")
        rt._gate.feed(np.full(1600, .1, dtype=np.float32))
        await settle(rt)
        assert rt.engine.state.value == "listening"
        assert rt.tts.chimes == 0
        assert rt.tts.spoken == []

    async def test_exit_phrase_after_wake_in_dnd_restores(self, tmp_path):
        """勿扰聆听窗内退出短语 → 退勿扰 + 确认话术出声（豁免口）。"""
        rig = make_rig(tmp_path, clock=lambda: 1000.0)
        rt = rig.runtime
        rt.engine.dnd = True
        rt.wake_stream.hits.append("小鹿")
        rt._gate.feed(np.full(1600, .1, dtype=np.float32))
        await settle(rt)
        rt.asr_stream.text = lambda: "退出勿扰模式"
        rt.asr_stream.is_endpoint = lambda: True
        rt._gate.feed(np.zeros(12000, dtype=np.float32))
        await settle(rt)
        assert rt.engine.dnd is False
        assert any("勿扰" in s for s, _, _ in rt.tts.spoken)

    async def test_passive_announcer_gated_in_dnd(self, tmp_path):
        """裁定 7：勿扰中被动播报源（sync 快照禁用公告）一律静音。"""
        rig = make_rig(tmp_path)
        rt = rig.runtime
        rt.engine.dnd = True
        announcer = make_snapshot_announcer(rt.store, rt.callbacks.speak)
        announcer(None, {"台灯": True})
        await settle(rt)
        assert rt.tts.spoken == [], "被动播报在勿扰中必须静音"

    async def test_status_reports_dnd(self, tmp_path):
        """勿扰标志对 dev 控制台/status 可见（最小接线；不含 Web 管理界面）。"""
        rig = make_rig(tmp_path)
        rt = rig.runtime
        assert rt.status()["dnd"] is False
        rt.engine.dnd = True
        assert rt.status()["dnd"] is True


# ---------- 全局音量持久化（2026-10-06 计划 U8） ----------


class TestGlobalVolume:
    async def test_voice_volume_adjusts_and_persists_via_hook(self, tmp_path):
        """语音调音量 → settings 变更 + 钩子落盘（三写点统一经 set_*）。"""
        from lira.settings import DeviceSettings

        persisted = []
        settings = DeviceSettings(on_change=lambda k, v: persisted.append((k, v)))
        rig = make_rig(tmp_path, settings=settings)
        rt = rig.runtime
        rt.callbacks.adjust_volume(0.2)
        assert rt.settings.volume == 1.2
        assert persisted == [("volume", 1.2)]

    async def test_adjust_volume_clamps_at_ceiling(self, tmp_path):
        from lira.settings import DeviceSettings

        persisted = []
        settings = DeviceSettings(volume=2.0, on_change=lambda k, v: persisted.append((k, v)))
        rig = make_rig(tmp_path, settings=settings)
        rt = rig.runtime
        rt.callbacks.adjust_volume(0.2)
        assert rt.settings.volume == 2.0, "顶格夹取不越界"
        assert persisted == [], "夹取后值未变不触发落盘"

    async def test_whitelist_volume_word_writes_global(self, tmp_path):
        """R16 语义并轨：朗读白名单「大声点」同步写全局持久值。"""
        from lira.settings import DeviceSettings

        persisted = []
        settings = DeviceSettings(on_change=lambda k, v: persisted.append((k, v)))
        rig = make_rig(tmp_path, settings=settings)
        rt = rig.runtime
        rt.callbacks.playback_volume_up()
        assert rt.settings.volume == 1.2, "白名单词改写全局值"
        assert persisted == [("volume", 1.2)]

    def test_settings_builder_roundtrip(self, tmp_path):
        """生产装配助手：写 → 重建 → 恢复。"""
        from lira.appliances.store import ApplianceStore
        from lira.main import build_settings_with_persistence

        store = ApplianceStore(tmp_path / "s.db")
        s1 = build_settings_with_persistence(store)
        s1.set_volume(0.8)
        s2 = build_settings_with_persistence(store)
        assert s2.volume == 0.8, "重启恢复持久音量"
        store.close()


class TestStatusFactsWiring:
    async def test_status_facts_production_mapping(self, tmp_path):
        """R17：生产 status_facts 映射（网络探针/隐私门/熔断器）。"""
        rig = make_rig(tmp_path)
        rt = rig.runtime
        facts = rt.callbacks.status_facts()
        assert isinstance(facts["network"], bool)
        assert facts["mic_on"] is True, "隐私关 → 麦克风开"
        assert facts["cloud"] in (True, False, None)


class TestReviewFixesWiring:
    async def test_persist_debounce_async_path_and_flush(self, tmp_path):
        """REL-002/TST-004. 事件循环内去抖：未到期不落盘、到期落盘、停机冲刷立即落盘。"""
        from lira.appliances.store import ApplianceStore
        from lira.main import build_settings_with_persistence

        store = ApplianceStore(tmp_path / "s.db")
        settings = build_settings_with_persistence(store, persist_delay=0.1)
        settings.set_volume(1.4)
        assert store.get_meta("volume") is None, "去抖窗口内不落盘"
        await asyncio.sleep(0.25)
        assert store.get_meta("volume") == "1.4", "到期落盘一次"
        settings.set_volume(0.8)
        settings.flush_persist()
        assert store.get_meta("volume") == "0.8", "停机冲刷立即落盘"
        store.close()

    async def test_volume_floor_skips_pipeline_side(self, tmp_path):
        """ADV-06. 全局下边界：白名单「小声点」夹取 no-op 时不得驱动会话侧变响。"""
        from lira.settings import DeviceSettings

        settings = DeviceSettings(volume=0.1)
        rig = make_rig(tmp_path, settings=settings)
        rt = rig.runtime
        calls = []
        rt.pipeline.playback_volume_down = lambda: calls.append("down")
        rt.callbacks.playback_volume_down()
        assert rt.settings.volume == 0.1, "全局已到底不调整"
        assert calls == [], "夹取 no-op 不得驱动会话侧（下边界会说小反响）"

    def test_status_reports_volume(self, tmp_path):
        """agent-native：语音可变的持久音量进入 status 观测面。"""
        rig = make_rig(tmp_path)
        assert rig.runtime.status()["volume"] == 1.0


# ---------- 聊天装配与降级（home-console 计划 U6） ----------


class TestChatAssembly:
    async def test_ui_mount_registers_chat_and_keeps_existing_assembly(
        self, tmp_path, monkeypatch
    ):
        """装配冒烟：mock HAL 下 UI 应用含聊天路由且 chat 服务真实可用，
        既有装配不变（/status 照常）；ChatStore 随 UI 伴随任务退出关闭。"""
        import sqlite3

        import httpx

        from lira.chat.store import ChatStore

        # 钉住 close() 接线：包一层记录调用（不改行为）
        closed: list[bool] = []
        original_close = ChatStore.close

        def recording_close(self):
            original_close(self)
            closed.append(True)

        monkeypatch.setattr(ChatStore, "close", recording_close)
        rig = make_rig(tmp_path, ui_port=0)
        task = asyncio.ensure_future(rig.runtime.run())
        try:
            await asyncio.sleep(0.3)
            server = rig.runtime._ui_server
            assert server is not None and server.started
            port = server.servers[0].sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient() as client:
                # 既有装配不变：/status 只读 JSON 照常（mock rig 已录入 1 台家电）
                r = await client.get(f"{base}/status")
                assert r.status_code == 200
                assert r.json()["appliance_count"] == 1
                # 聊天路由已注册：选人页 200（chat 未装配时建成员会得显式 503）
                picker = await client.get(f"{base}/chat")
                assert picker.status_code == 200
                # 经生产装配的 ChatService 真实落库：创建成员 → 303 进会话页
                created = await client.post(f"{base}/chat/members", data={"name": "妈妈"})
                assert created.status_code == 303
                assert created.headers["location"].startswith("/chat/")
                # R5 装配接线证明：消息端点走生产 ChatService——remote_available
                # 与 LlmClient.is_available 同源（FakeLlm 恒 False → unavailable；
                # FakeLlm 无 .chat，若谓词未接线而误发起真实调用会 500 使测试失败）
                mid = created.headers["location"].rsplit("/", 1)[-1]
                sent = await client.post(f"{base}/chat/{mid}/messages", json={"text": "你好"})
                assert sent.status_code == 200
                assert sent.json()["state"] == "unavailable"
                # 隐私谓词与主循环同一 PrivacyState：开启后隐私拦截优先于可用性
                # 检查（同样零 LLM 调用，user 消息按 R7 单独落库）
                await rig.runtime.privacy.set_enabled(True, source="test")
                sent_privacy = await client.post(
                    f"{base}/chat/{mid}/messages", json={"text": "你好"}
                )
                assert sent_privacy.status_code == 200
                assert sent_privacy.json()["state"] == "privacy"
                # 会话页可达且选人页列出该成员（store 读写经同一装配的 ChatService）
                convo = await client.get(f"{base}{created.headers['location']}")
                assert convo.status_code == 200 and "妈妈" in convo.text
                after = await client.get(f"{base}/chat")
                assert after.status_code == 200 and "妈妈" in after.text
        finally:
            # 还原隐私关闭（幂等：本就关闭时无广播）：状态改动不外溢到其他测试
            await rig.runtime.privacy.set_enabled(False, source="test")
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)
        assert len(closed) == 1, "ChatStore.close() 必须挂在 _run_ui 退出路径"
        # db 路径核对：聊天表建在既有设备库文件（无第三份数据副本）
        with sqlite3.connect(tmp_path / "device.db") as probe:
            tables = {
                row[0]
                for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"members", "messages"} <= tables

    async def test_chat_store_failure_degrades_to_none(self, tmp_path, monkeypatch, caplog):
        """降级路径：ChatStore 构造失败 → 警告（无内容）+ chat=None，
        UI 仍挂载、既有面板不受影响，异常不上抛拖垮主循环。"""
        import sqlite3

        import httpx

        from lira.chat.store import ChatStore

        def broken_init(self, *args, **kwargs):
            raise sqlite3.OperationalError("chat store init boom")

        monkeypatch.setattr(ChatStore, "__init__", broken_init)
        rig = make_rig(tmp_path, ui_port=0)
        with caplog.at_level(logging.WARNING):
            task = asyncio.ensure_future(rig.runtime.run())
            try:
                await asyncio.sleep(0.3)
                server = rig.runtime._ui_server
                assert server is not None and server.started, "聊天失败不得拖垮 UI"
                port = server.servers[0].sockets[0].getsockname()[1]
                base = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient() as client:
                    # 既有面板不受影响：/status 与首页（含隐私开关入口）照常
                    r = await client.get(f"{base}/status")
                    assert r.status_code == 200
                    home = await client.get(f"{base}/")
                    assert home.status_code == 200
                    # 聊天入口显式"暂不可用"（不隐藏、不静默、不报错页）
                    assert "暂不可用" in home.text
            finally:
                rig.source.push_eof()
                await asyncio.wait_for(task, timeout=5.0)
        assert any("chat_init_failed" in rec.message for rec in caplog.records)


# ---------- U6/KTD5：声纹候选装配与观察接线（LIRA_PERSONAL_MEMORY=1 分支） ----------


class FakePersonalMemory:
    """PersonalMemory 替身：只提供装配面用到的属性（db / profiles）。"""

    def __init__(self, db):
        self.db = db

    def profiles(self):
        return []


class FakeSpeakerIdentity:
    """SpeakerIdentity 替身：固定向量 + 固定指纹（不触 sherpa）。"""

    model_fingerprint = "test-fp:2"

    def __init__(self):
        self.pending = None

    def embedding(self, samples):
        return np.array([1.0, 0.0], np.float32)

    def match(self, vector):
        return None

    def resolve(self, text, vector):
        return None, None, None


class RecordingCandidates:
    """VoiceCandidates 替身：记录构造参数与 observe 调用（判定逻辑自有测试）。"""

    instances: list["RecordingCandidates"] = []

    def __init__(self, db, *, fingerprint, profiles_provider, params, privacy,
                 now=None, event_sink=None):
        self.db = db
        self.fingerprint = fingerprint
        self.profiles_provider = profiles_provider
        self.params = params
        self.privacy = privacy
        self.event_sink = event_sink
        self.observed: list = []
        RecordingCandidates.instances.append(self)

    def observe(self, vector, samples):
        self.observed.append((vector, samples))
        return None


def make_personal_rig(tmp_path, monkeypatch):
    """LIRA_PERSONAL_MEMORY=1 装配台架：memory/identity/候选全部替身化。"""
    import sqlite3

    import lira.personal_memory as pm_mod
    import lira.speaker_candidates as sc_mod

    db = sqlite3.connect(":memory:")
    monkeypatch.setattr(pm_mod, "PersonalMemory", lambda *a, **k: FakePersonalMemory(db))
    monkeypatch.setattr(pm_mod, "SpeakerIdentity", lambda *a, **k: FakeSpeakerIdentity())
    RecordingCandidates.instances = []
    monkeypatch.setattr(sc_mod, "VoiceCandidates", RecordingCandidates)
    monkeypatch.setenv("LIRA_PERSONAL_MEMORY", "1")
    rig = make_rig(tmp_path)
    return rig, RecordingCandidates.instances[-1]


class TestSpeakerCandidatesAssembly:
    async def test_personal_memory_env_wires_candidates_to_shared_connection(
            self, tmp_path, monkeypatch):
        """装配面（U7 消费入口）：候选与 PersonalMemory 同一连接、参数来自
        cfg.speaker、指纹来自 SpeakerIdentity、隐私谓词与主循环同源。"""
        rig, candidates = make_personal_rig(tmp_path, monkeypatch)
        rt = rig.runtime
        assert rt.speaker_candidates is candidates
        assert candidates.db is rt.personal_memory.db, "必须同一 SQLite 连接（KTD6 晋升同事务）"
        assert candidates.fingerprint == "test-fp:2"
        assert candidates.params is rt._cfg.speaker
        assert candidates.profiles_provider() == rt.personal_memory.profiles()
        assert candidates.privacy() is rt.privacy.is_on
        assert candidates.event_sink is not None, "观察事件面进 dev 遥测（U7 辅助数据源）"

    async def test_handle_voice_input_observes_after_post_extraction_recheck(
            self, tmp_path, monkeypatch):
        """提取后复检通过 → observe 被调（向量与样本透传；resolve 之前）。"""
        from lira.main import DeviceRuntime

        rig, candidates = make_personal_rig(tmp_path, monkeypatch)
        rt = rig.runtime
        await rt.engine.on_wake()
        samples = np.full(16000, 0.2, dtype=np.float32)
        await DeviceRuntime.handle_voice_input(
            rt, "现在几点了", samples, rt.dispatcher.generation, False)
        assert len(candidates.observed) == 1
        vector, observed_samples = candidates.observed[0]
        assert np.allclose(vector, np.array([1.0, 0.0], np.float32))
        assert observed_samples.shape == samples.shape

    async def test_handle_voice_input_skips_observe_on_stale_generation(
            self, tmp_path, monkeypatch):
        """AE5：提取期间 generation 轮换（隐私开关两次）→ 晚到结果不提交。"""
        from lira.main import DeviceRuntime

        rig, candidates = make_personal_rig(tmp_path, monkeypatch)
        rt = rig.runtime
        await rt.engine.on_wake()
        stale_generation = rt.dispatcher.generation
        # 提取期间 generation 轮换：privacy on/off 各 bump 一次 dispatcher 代次
        await rt.privacy.set_enabled(True, source="test")
        await rt.privacy.set_enabled(False, source="test")
        await rt.engine.on_wake()  # 回到聆听态，唯独 generation 已旧
        await DeviceRuntime.handle_voice_input(
            rt, "现在几点了", np.full(16000, 0.2, dtype=np.float32), stale_generation, False)
        assert candidates.observed == [], "过期代次的提取结果不得进入候选积累"

    async def test_handle_voice_input_skips_observe_without_samples(
            self, tmp_path, monkeypatch):
        """空样本（命令通道兜底 np.empty(0)）与提取失败（vector None）不观察。"""
        from lira.main import DeviceRuntime

        rig, candidates = make_personal_rig(tmp_path, monkeypatch)
        rt = rig.runtime
        await rt.engine.on_wake()
        generation = rt.dispatcher.generation
        await DeviceRuntime.handle_voice_input(rt, "现在几点了", np.empty(0, np.float32),
                                               generation, False)
        assert candidates.observed == []
        rt.speaker_identity.embedding = lambda samples: None  # 提取失败：向量 None
        await DeviceRuntime.handle_voice_input(rt, "现在几点了",
                                               np.full(16000, 0.2, np.float32),
                                               generation, False)
        assert candidates.observed == []

    async def test_feature_off_keeps_attribute_none(self, tmp_path, monkeypatch):
        """默认（LIRA_PERSONAL_MEMORY!=1）：speaker_candidates=None，主链路零变化。"""
        monkeypatch.delenv("LIRA_PERSONAL_MEMORY", raising=False)
        rig = make_rig(tmp_path)
        assert rig.runtime.speaker_candidates is None
        assert rig.runtime.personal_memory is None


# ---------- U3/KTD3：同轮观测全链路（dispatcher→engine→callbacks→遥测） ----------


class TestTurnObservationChain:
    """生产装配下的同轮观测：utterance→turn→route→speak→speak_done 关联链，
    且本地家电指令零模型事件（AE3）。"""

    async def test_local_appliance_chain_observed_without_llm_events(self, tmp_path):
        rig = make_rig(tmp_path)
        rt = rig.runtime
        task = asyncio.ensure_future(rt.run())  # ir_service 在 run() 进入 HAL 后装配
        try:
            await asyncio.sleep(0.05)
            tel = rt.telemetry
            await rt.engine.on_wake()
            delivery_generation = rt.dispatcher.generation  # 投递时的采集代次
            rt.asr_stream.text = lambda: "打开台灯"
            rt.asr_stream.is_endpoint = lambda: True
            # 单次喂入（假 ASR 流 text/endpoint 恒定，多块会重复投递同一句）
            rt._gate.feed(np.zeros(4000, dtype=np.float32))
            await settle(rt)
            assert rig.ir.sent_codes == ["CODE_LAMP_ON"]
            events = tel.events_since(0)[0]
            by_kind: dict[str, list] = {}
            for e in events:
                by_kind.setdefault(e.kind, []).append(e)
            utterances = by_kind.get("utterance", [])
            turns = by_kind.get("turn", [])
            routes = by_kind.get("route", [])
            speaks = by_kind.get("speak", [])
            dones = by_kind.get("speak_done", [])
            assert [e.text for e in utterances] == ["打开台灯"]
            assert [e.text for e in turns] == ["打开台灯"]
            assert [e.text for e in routes] == ["local_rule"]
            assert speaks
            # 关联链：utterance→turn→route 同轮串联（U5 渲染依据）。本地规则轮
            # 的收尾走既有状态迁移镜像（speak_done 按设计只挂 _speak_answer
            # 既有成功/失败判定处，模型回答轮才有）。
            assert dones == []
            assert rt.engine.state.value == "standby"
            assert turns[0].utterance_id == utterances[0].utterance_id
            assert routes[0].turn_id == turns[0].turn_id
            assert utterances[0].capture_generation == delivery_generation
            assert any(s.operation_owner for s in speaks)  # speak 带 owner 关联
            # 本地指令全程无模型事件（无 llm_request/first_delta/done/fail/cancel）
            assert not [e for e in events if e.kind.startswith("llm_")]
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_command_channel_utterance_is_prefixed_and_correlated(self, tmp_path):
        """命令 KWS 通道投递：utterance 事件带来源前缀，turn 正常关联。"""
        rig = make_rig(tmp_path)
        rt = rig.runtime
        await rt.engine.on_wake()
        rt.command_stream.hits.append("闭嘴")  # COMMAND_KWS_WORDS 声学子集
        rt._gate.feed(np.zeros(1600, dtype=np.float32))
        await settle(rt)
        events = rt.telemetry.events_since(0)[0]
        utterances = [e for e in events if e.kind == "utterance"]
        assert [e.text for e in utterances] == ["命令通道：闭嘴"]
        turns = [e for e in events if e.kind == "turn"]
        assert [e.text for e in turns] == ["闭嘴"]
        assert turns[0].utterance_id == utterances[0].utterance_id
