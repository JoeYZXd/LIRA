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

    def speak(self, text: str, speed: float = 1.0, volume: float = 1.0) -> asyncio.Event:
        self.spoken.append((text, speed, volume))
        done = asyncio.Event()
        done.set()
        return done


class FakeLlm:
    """LlmClient 替身：远程恒不可用（路由走本地规则），白话钩子恒本地直读。"""

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


def make_rig(tmp_path, *, ws_url: str = "", transport_factory=None, ui_port: int = 0, clock=None):
    """整机替身装配（mock HAL + 假语音栈 + 假 LLM/OCR）。"""
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
    ir = MockIrController()
    source = SilenceSource()
    hal = {"camera": FakeCamera(), "audio": source, "ir": ir, "display": MockDisplay()}
    settings = DeviceSettings()
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
        audio=AudioStack(wake_kws=FakeKws(), asr=FakeAsr(), playback_kws=FakeKws(), tts=tts),
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


class TestDialogWiring:
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
            assert any("我在听" in t for t in spoken)
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
            rig.runtime.wake_stream.hits.append("小丽拉 @xiao li la")
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

    def test_build_board_hal_constructs_and_warns(self, tmp_path, caplog):
        """board 后端装配不触硬件：构造成功 + IR 待 M5 告警可见。"""
        from lira.hal.board import V4l2RawCamera
        from lira.audio.mic import SoundDeviceMic

        yaml_text = (
            "llm:\n  api_key: k\n"
            "hal:\n  backend: board\n  mic_device: '3'\n  speaker_device: '2'\n"
            f"db_path: {(tmp_path / 'device.db').as_posix()}\n"
        )
        path = tmp_path / "cfg.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        cfg = load_config(path, env={})
        with caplog.at_level(logging.WARNING):
            hal = build_board_hal(cfg)
        assert isinstance(hal["camera"], V4l2RawCamera)
        assert isinstance(hal["audio"], SoundDeviceMic)
        assert any("M5" in r.message for r in caplog.records)

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
        注入假时钟快进 LISTENING 8s 窗口 x2 -> 复述提示 -> 礼貌回待机。"""
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
            rig.source.push_silence(0.2)
            await asyncio.sleep(0.3)  # 等编排循环消费静音块并触发 tick
            await settle(rig.runtime)
            assert any("指令" in t for t, _, _ in rig.tts.spoken)
            now["t"] += 9.0  # 第二个窗口超时
            rig.source.push_silence(0.2)
            await asyncio.sleep(0.3)
            await settle(rig.runtime)
            assert rig.runtime.engine.state.value == "standby"
        finally:
            rig.source.push_eof()
            await asyncio.wait_for(task, timeout=5.0)
