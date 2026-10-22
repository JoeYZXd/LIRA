"""语音交互实时日志测试（lira.voice_log + 状态机/路由分发/LLM 客户端挂钩点）。

覆盖：环形容量挤出、状态流转文案（含 STANDBY 来源二分）、路由分发三路
（唤醒/ASR 切句/播放白名单）、LLM chat 四路（ok/隐私拦截/连接失败）、
dev 端点鉴权与读写。单例 VOICE_LOG 由 autouse fixture 复位。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from lira.appliances.store import ApplianceStore
from lira.config import LlmConfig
from lira.dialog import AudioRoute, DialogEngine, Router, State
from lira.dialog.intents import Appliance
from lira.dialog.route_dispatch import RouteDispatch
from lira.dialog.state_machine import _voice_log_state
from lira.llm import LlmClient, LlmError, PrivacyBlocked
from lira.privacy import PassphraseVault, PrivacyState
from lira.settings import DeviceSettings
from lira.ui.app import UiServices, create_app
from lira.ui.dev import DevHandle
from lira.voice_log import VOICE_LOG, VoiceLog

BASE_URL = "http://unit.test/v1"
PASSPHRASE = "test-passphrase"


@pytest.fixture(autouse=True)
def _clean_singleton():
    """单例 VOICE_LOG 测试间复位（挂钩点直接写单例）。"""
    VOICE_LOG.clear()
    yield
    VOICE_LOG.clear()


def _texts():
    return [e.text for e in VOICE_LOG.snapshot()]


# ---------- 环形区本体 ----------


class TestVoiceLogRing:
    def test_capacity_evicts_oldest(self):
        ring = VoiceLog(capacity=3)
        for i in range(5):
            ring.log("state", f"e{i}")
        entries = ring.snapshot()
        assert [e.text for e in entries] == ["e2", "e3", "e4"]

    def test_clear(self):
        ring = VoiceLog()
        ring.log("wake", "x")
        ring.clear()
        assert ring.snapshot() == []


# ---------- 状态机挂钩 ----------


class _Cb:
    """最小回调桩（Recorder 同构，只记录本测试关心的面）。"""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.ir: list[tuple[str, str]] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def start_listening(self) -> None:
        pass

    def set_audio_route(self, route: AudioRoute) -> None:
        pass

    def start_capture(self):
        return asyncio.ensure_future(asyncio.sleep(0))

    def send_ir(self, device: str, action: str) -> None:
        self.ir.append((device, action))

    def playback_pause(self) -> None: ...
    def playback_resume(self) -> None: ...
    def playback_stop(self) -> None: ...
    def playback_read_again(self) -> None: ...
    def playback_volume_up(self) -> None: ...
    def playback_volume_down(self) -> None: ...


APPLIANCES = (
    Appliance(
        name="取暖器",
        aliases=("取暖器",),
        actions={"打开": ("打开",), "关闭": ("关闭",)},
        is_high_risk=True,
    ),
    Appliance(
        name="台灯",
        aliases=("台灯",),
        actions={"打开": ("打开",), "关闭": ("关闭",)},
        is_high_risk=False,
    ),
)


class TestStateMachineEntries:
    def _engine(self) -> DialogEngine:
        return DialogEngine(Router(APPLIANCES), _Cb())

    async def test_initial_standby_label(self):
        self._engine()
        assert _texts() == ["待机：持续等待唤醒中"]

    async def test_wake_listen_execute_standby_flow(self):
        engine = self._engine()
        await engine.on_wake()
        await engine.on_asr_text("打开台灯")
        assert _texts() == [
            "待机：持续等待唤醒中",
            "聆听：等待语音指令",
            "正在理解指令",
            "指令执行中",
            "退出唤醒状态，回到待机，等待下次唤醒",
        ]

    async def test_high_risk_confirming_label(self):
        engine = self._engine()
        await engine.on_wake()
        await engine.on_asr_text("打开取暖器")
        assert _texts()[-1] == "高危设备：等待语音确认"
        await engine.on_asr_text("确认")
        assert _texts()[-1] == "退出唤醒状态，回到待机，等待下次唤醒"

    def test_capture_reading_labels(self):
        """CAPTURING/READING 标签直达映射（引擎级流程由 e2e/阅读测试覆盖）。"""
        _voice_log_state(State.CAPTURING, initial=False)
        _voice_log_state(State.READING, initial=False)
        assert _texts() == ["拍摄取书中", "朗读中"]


# ---------- 路由分发挂钩 ----------


class _KwsStub:
    def __init__(self, *script: str) -> None:
        self._script = list(script)

    def feed_and_poll(self, samples):
        return self._script.pop(0) if self._script else None


class _AsrStub:
    def __init__(self, text: str) -> None:
        self._text = text
        self._fed = False

    def feed(self, samples) -> None:
        self._fed = True

    def text(self) -> str:
        return self._text if self._fed else ""

    def is_endpoint(self) -> bool:
        return self._fed

    def reset(self) -> None:
        self._fed = False


class _Holder:
    def __init__(self, route: AudioRoute, **streams) -> None:
        self.route = route
        self.engine = DialogEngine(Router(APPLIANCES), _Cb())
        self.wake_stream = streams.get("wake")
        self.asr_stream = streams.get("asr")
        self.playback_stream = streams.get("playback")


class TestRouteDispatchEntries:
    SAMPLES = b"\x00\x00" * 160

    async def test_failed_wake_releases_pending_for_next_hit(self):
        holder = _Holder(AudioRoute.WAKE, wake=_KwsStub("小鹿", "小鹿"))
        async def failed_wake():
            raise RuntimeError("stop failed")
        holder.engine.on_wake = failed_wake
        pending = []
        dispatch = RouteDispatch(streams=holder, wakeword="小鹿", spawn=pending.append)
        dispatch.feed(self.SAMPLES)
        with pytest.raises(RuntimeError):
            await pending.pop()
        dispatch.feed(self.SAMPLES)
        assert len(pending) == 1
        pending.pop().close()

    def test_audio_without_wake_reports_level_once_per_second(self):
        holder = _Holder(AudioRoute.WAKE, wake=_KwsStub(None))
        dispatch = RouteDispatch(streams=holder, wakeword="小丽拉", spawn=lambda coro: None)
        for _ in range(100):
            dispatch.feed(self.SAMPLES)
        audio = [e.text for e in VOICE_LOG.snapshot() if e.kind == "audio"]
        assert len(audio) == 1
        assert "RMS=-200.0 dBFS" in audio[0]
        assert "未报告新命中" in audio[0]
        assert not any(e.kind == "wake" for e in VOICE_LOG.snapshot())

    def test_wrong_keyword_explains_rejection(self):
        holder = _Holder(AudioRoute.WAKE, wake=_KwsStub("其他词"))
        dispatch = RouteDispatch(streams=holder, wakeword="小丽拉", spawn=lambda coro: None)
        dispatch.feed(self.SAMPLES)
        assert any("不匹配，不触发唤醒" in t for t in _texts())
        assert not any(e.kind == "wake" for e in VOICE_LOG.snapshot())

    async def test_wake_hit_logged(self):
        holder = _Holder(AudioRoute.WAKE, wake=_KwsStub("xiǎolìlā@小丽拉"))
        dispatch = RouteDispatch(
            streams=holder, wakeword="小丽拉",
            spawn=lambda coro: asyncio.ensure_future(coro),
        )
        dispatch.feed(self.SAMPLES)
        await asyncio.sleep(0)  # 让 spawn 的 on_wake 落地
        texts = _texts()
        assert "收到语音唤醒（小丽拉）" in texts
        assert "聆听：等待语音指令" in texts

    async def test_asr_endpoint_logged(self):
        holder = _Holder(AudioRoute.LISTEN, asr=_AsrStub("打开台灯"))
        dispatch = RouteDispatch(
            streams=holder, wakeword="小丽拉",
            spawn=lambda coro: asyncio.ensure_future(coro),
        )
        dispatch.feed(self.SAMPLES)
        await asyncio.sleep(0)
        assert "识别到指令：打开台灯" in _texts()
        assert "ASR 中间结果（未提交）：打开台灯" in _texts()

    async def test_playback_whitelist_logged(self):
        holder = _Holder(AudioRoute.PLAYBACK, playback=_KwsStub("zàntíng@暂停"))
        dispatch = RouteDispatch(
            streams=holder, wakeword="小丽拉",
            spawn=lambda coro: asyncio.ensure_future(coro),
        )
        dispatch.feed(self.SAMPLES)
        await asyncio.sleep(0)
        assert "播放控制语音：暂停" in _texts()


# ---------- LLM 客户端挂钩 ----------


def _sse_body(*deltas: str) -> bytes:
    import json

    lines = []
    base = {"id": "c1", "object": "chat.completion.chunk",
            "created": 1700000000, "model": "unit-model"}
    for delta in deltas:
        chunk = {**base, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    lines.append("data: " + json.dumps(final, ensure_ascii=False) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


class TestLlmClientEntries:
    def _client(self, handler) -> LlmClient:
        oai = AsyncOpenAI(
            api_key="sk-test", base_url=BASE_URL,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )
        return LlmClient(
            LlmConfig(
                base_url=BASE_URL, api_key="sk-test", model="unit-model",
                timeout_seconds=10.0, breaker_failure_threshold=3,
                breaker_cooldown_seconds=60.0, wait_feedback_seconds=2.0,
                colloquial_threshold_chars=150,
            ),
            openai_client=oai,
        )

    async def test_ok_logs_start_and_reply(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=_sse_body("你", "好"),
            )

        reply = await self._client(handler).chat("在吗")
        assert reply == "你好"
        assert _texts() == [
            "与大模型交互中（提示 2 字）…",
            "大模型返回结果（2 字）：你好",
        ]

    async def test_privacy_blocked_logged(self):
        client = LlmClient(
            LlmConfig(
                base_url=BASE_URL, api_key="sk-test", model="unit-model",
                timeout_seconds=10.0, breaker_failure_threshold=3,
                breaker_cooldown_seconds=60.0, wait_feedback_seconds=2.0,
                colloquial_threshold_chars=150,
            ),
            privacy=lambda: True,
        )
        with pytest.raises(PrivacyBlocked):
            await client.chat("在吗")
        assert _texts() == ["隐私模式开启，远程调用已拦截"]

    async def test_conn_error_logged(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = self._client(handler)
        with pytest.raises(LlmError):
            await client.chat("在吗")
        assert _texts()[-1] == "大模型连接失败，已降级"


# ---------- dev 控制台端点 ----------


class _FakeEngine:
    state = SimpleNamespace(value="standby")


class _FakeRuntime:
    def __init__(self) -> None:
        self.store = ApplianceStore(":memory:")
        self.privacy = PrivacyState()
        self.vault = PassphraseVault(self.store)
        self.settings = DeviceSettings()
        self.engine = _FakeEngine()


class TestDevEndpoints:
    def _client(self):
        rt = _FakeRuntime()
        rt.vault.set(PASSPHRASE)
        services = UiServices(
            privacy=rt.privacy, vault=rt.vault, store=rt.store,
            settings=rt.settings, dev=DevHandle(rt),
        )
        client = TestClient(create_app(services))
        return client

    def test_unauthenticated_401(self):
        client = self._client()
        assert client.get("/dev/api/voice-log").status_code == 401
        assert client.post("/dev/api/voice-log/clear", json={}).status_code == 401

    def test_entries_and_state_roundtrip(self):
        client = self._client()
        client.post("/dev/login", json={"passphrase": PASSPHRASE})
        VOICE_LOG.log("wake", "收到语音唤醒（小丽拉）")
        VOICE_LOG.log("asr", "识别到指令：打开台灯")
        body = client.get("/dev/api/voice-log").json()
        assert body["state"] == "standby"
        kinds = [e["kind"] for e in body["entries"]]
        assert kinds == ["wake", "asr"]
        assert "小丽拉" in body["entries"][0]["text"]
        assert client.post("/dev/api/voice-log/clear", json={}).status_code == 200
        assert client.get("/dev/api/voice-log").json()["entries"] == []

    def test_page_contains_section(self):
        """dev 页保留语音交互日志展示面（U5 起：实时区日志工作区的语音事件 tab）。"""
        client = self._client()
        client.post("/dev/login", json={"passphrase": PASSPHRASE})
        page = client.get("/dev").text
        assert "实时区：声音 · 同轮交互 · 日志" in page
        assert "语音事件" in page
