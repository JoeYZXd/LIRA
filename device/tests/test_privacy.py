"""U7 隐私模式测试（R12，AE1 设备侧基元）。

覆盖：
  - PrivacyState 状态广播（订阅次序 = 装配次序；幂等；事件审计）
  - PrivacyGatedSink：隐私 ON 时音频喂入丢弃（KWS/ASR 零输入，"KWS 不命中"
    由构造保证——mock 音频注入喂入计数断言）
  - 状态机唤醒门：隐私 ON → on_wake 忽略（不可唤醒）；OFF → 恢复
  - LLM 谓词联动：privacy ON → PrivacyBlocked 且网络层零请求（复用 U5 注入点）
  - 事件广播次序：关麦订阅者先于播报订阅者执行
  - PassphraseVault：scrypt 哈希存储、无默认值、验证成功/失败
"""

from __future__ import annotations

import json

import httpx2 as httpx
import numpy as np
import pytest
from openai import AsyncOpenAI

from lira.appliances.store import ApplianceStore
from lira.config import LlmConfig
from lira.dialog import DialogEngine, Router, State, phrasebook as pb
from lira.dialog.intents import Appliance
from lira.llm import LlmClient, PrivacyBlocked
from lira.privacy import (
    PassphraseVault,
    PrivacyGatedSink,
    PrivacyState,
    attach_announcer,
)

# ---------- 测试基建 ----------


class RecordingSink:
    """AudioSink 记录器：断言喂入是否穿透门。"""

    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []

    def feed(self, samples: np.ndarray) -> None:
        self.chunks.append(samples)


class StateMachineRecorder:
    """最小 DialogCallbacks 记录器（U7 只关心唤醒/播报）。"""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.routes: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def start_listening(self) -> None:
        pass

    def set_audio_route(self, route) -> None:
        self.routes.append(route.value)

    def start_capture(self) -> None:
        pass

    def send_ir(self, device: str, action: str) -> None:
        pass

    def playback_pause(self) -> None: ...

    def playback_resume(self) -> None: ...

    def playback_stop(self) -> None: ...

    def playback_read_again(self) -> None: ...

    def playback_volume_up(self) -> None: ...

    def playback_volume_down(self) -> None: ...


def make_engine(privacy: PrivacyState) -> tuple[DialogEngine, StateMachineRecorder]:
    rec = StateMachineRecorder()
    engine = DialogEngine(
        router=Router(appliances=()),
        callbacks=rec,
        wake_allowed=lambda: not privacy.is_on,
    )
    return engine, rec


_SSE_BODY = (
    'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}\n\n'
    'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
).encode("utf-8")


def make_llm(privacy: PrivacyState) -> tuple[LlmClient, list[httpx.Request]]:
    """装配 LlmClient，privacy 谓词直接注入 PrivacyState 对象本身。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_SSE_BODY,
        )

    oai = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://unit.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    cfg = LlmConfig(
        base_url="http://unit.test/v1",
        api_key="sk-test",
        model="unit-model",
        timeout_seconds=10.0,
        breaker_failure_threshold=3,
        breaker_cooldown_seconds=60.0,
        wait_feedback_seconds=2.0,
        colloquial_threshold_chars=150,
    )
    return LlmClient(cfg, openai_client=oai, privacy=privacy), requests


@pytest.fixture
def privacy() -> PrivacyState:
    return PrivacyState()


@pytest.fixture
def store(tmp_path) -> ApplianceStore:
    s = ApplianceStore(tmp_path / "device.db")
    yield s
    s.close()


# ---------- PrivacyState 状态广播 ----------


class TestPrivacyState:
    async def test_initial_off_and_predicate_callable(self, privacy):
        assert privacy.is_on is False
        assert privacy() is False  # LlmClient privacy= 谓词形态

    async def test_subscribers_called_in_registration_order(self, privacy):
        order: list[str] = []
        privacy.subscribe(lambda on: order.append("mic"))
        privacy.subscribe(lambda on: order.append("wake"))
        privacy.subscribe(lambda on: order.append("announce"))

        assert await privacy.set_enabled(True, source="test") is True
        assert order == ["mic", "wake", "announce"], "广播次序 = 注册次序"

    async def test_async_subscriber_supported(self, privacy):
        seen: list[bool] = []

        async def sub(on: bool) -> None:
            seen.append(on)

        privacy.subscribe(sub)
        await privacy.set_enabled(True, source="test")
        assert seen == [True]

    async def test_idempotent_set_no_broadcast_no_event(self, privacy):
        calls: list[bool] = []
        privacy.subscribe(calls.append)
        await privacy.set_enabled(False, source="test")  # 已是 off
        assert calls == []
        assert privacy.events == []
        await privacy.set_enabled(True, source="test")
        await privacy.set_enabled(True, source="test")  # 重复开启
        assert calls == [True]
        assert [e.enabled for e in privacy.events] == [True]

    async def test_events_audit_record_source_and_seq(self, privacy):
        await privacy.set_enabled(True, source="ui")
        await privacy.set_enabled(False, source="ui")
        assert [(e.source, e.enabled, e.seq) for e in privacy.events] == [
            ("ui", True, 0),
            ("ui", False, 1),
        ]


# ---------- 音频门（AE1：隐私 ON → KWS 不命中，构造保证） ----------


class TestPrivacyGatedSink:
    async def test_off_forwards_feed(self, privacy):
        inner = RecordingSink()
        gate = PrivacyGatedSink(inner, privacy)
        gate.feed(np.zeros(160, dtype=np.float32))
        assert len(inner.chunks) == 1

    async def test_on_drops_feed_zero_input_to_kws(self, privacy):
        """mock 音频注入：隐私 ON 期间喂入全部丢弃，KWS 流零输入 → 不可能命中。"""
        inner = RecordingSink()
        gate = PrivacyGatedSink(inner, privacy)
        privacy.subscribe(gate.apply)

        await privacy.set_enabled(True, source="test")
        for _ in range(5):
            gate.feed(np.ones(1600, dtype=np.float32))
        assert inner.chunks == [], "隐私期识别流必须零输入"
        assert gate.dropped_samples == 5 * 1600

        # 恢复后立即透传（隐私 OFF → 分发恢复）
        await privacy.set_enabled(False, source="ui")
        gate.feed(np.ones(1600, dtype=np.float32))
        assert len(inner.chunks) == 1


# ---------- 状态机唤醒门（R12：隐私期不可唤醒） ----------


class TestWakeGate:
    async def test_privacy_on_blocks_wake(self, privacy):
        engine, rec = make_engine(privacy)
        await privacy.set_enabled(True, source="test")
        await engine.on_wake()
        assert engine.state is State.STANDBY
        assert rec.spoken == [], "隐私期麦克风已关，不存在可播报通道"

    async def test_privacy_off_restores_wake_with_announcement(self, privacy):
        """隐私 OFF → 唤醒恢复 + 播报记录存在。"""
        spoken: list[str] = []
        attach_announcer(privacy, spoken.append)
        engine, rec = make_engine(privacy)

        await privacy.set_enabled(True, source="ui")
        await engine.on_wake()
        assert engine.state is State.STANDBY

        await privacy.set_enabled(False, source="ui")

        assert privacy.is_on is False
        assert spoken == [pb.PRIVACY_ON, pb.PRIVACY_OFF], "切换全程播报后果（开与关各一次）"
        await engine.on_wake()
        assert engine.state is State.LISTENING
        assert pb.WAKE_ACK in rec.spoken


# ---------- 事件广播次序（在途远端请求拦截次序） ----------


class TestBroadcastOrdering:
    async def test_mic_gated_before_announcement_spoken(self, privacy):
        """订阅次序纪律：先关麦、后播报——播报时门已闭合（装配层注册顺序约定）。"""
        order: list[str] = []
        inner = RecordingSink()
        gate = PrivacyGatedSink(inner, privacy)

        def mic_sub(on: bool) -> None:
            order.append("mic_gate")

        def announce(on: bool) -> None:
            order.append("announce")

        privacy.subscribe(mic_sub)
        privacy.subscribe(announce)
        await privacy.set_enabled(True, source="test")
        assert order == ["mic_gate", "announce"]
        assert inner.chunks == [], "播报发生时音频门已闭合"

    async def test_inflight_blocked_after_toggle_no_request(self, privacy):
        """隐私 ON 后下一次远端调用立即拦截、网络层零请求（在途请求由 U5 入口现查保证）。"""
        client, requests = make_llm(privacy)
        await privacy.set_enabled(True, source="ui")
        with pytest.raises(PrivacyBlocked):
            await client.chat("不应到达网络")
        assert requests == []


# ---------- LLM 谓词联动（复用 U5 注入点） ----------


class TestLlmPredicate:
    async def test_privacy_state_directly_injectable(self, privacy):
        """PrivacyState 本身即可作 U5 privacy= 谓词：off 放行、on 入口拦截。"""
        client, requests = make_llm(privacy)
        assert await client.chat("正常调用") == "好"
        assert len(requests) == 1
        await privacy.set_enabled(True, source="ui")
        with pytest.raises(PrivacyBlocked):
            await client.chat("被拦截")
        assert len(requests) == 1, "拦截发生在网络入口，零新请求"


# ---------- 播报话术 ----------


class TestAnnouncementPhrases:
    async def test_announcement_phrases_mention_mic_state(self):
        assert "麦克风" in pb.PRIVACY_ON
        assert "恢复" in pb.PRIVACY_OFF


# ---------- PassphraseVault（无默认值，scrypt 哈希存储） ----------


class TestPassphraseVault:
    def test_not_set_by_default(self, store):
        assert PassphraseVault(store).is_set() is False

    def test_set_then_verify(self, store):
        vault = PassphraseVault(store)
        vault.set("全家福2026")
        assert vault.is_set() is True
        assert vault.verify("全家福2026") is True
        assert vault.verify("全家福2026 ") is False
        assert vault.verify("wrong") is False

    def test_too_short_rejected_no_default_value(self, store):
        vault = PassphraseVault(store)
        with pytest.raises(ValueError):
            vault.set("abc")
        assert vault.is_set() is False

    def test_replace_password(self, store):
        vault = PassphraseVault(store)
        vault.set("old-password")
        vault.set("new-password")
        assert vault.verify("old-password") is False
        assert vault.verify("new-password") is True

    def test_stored_value_is_scrypt_hash_not_plaintext(self, store):
        vault = PassphraseVault(store)
        vault.set("s3cret-pass")
        stored = store.get_meta(PassphraseVault.META_KEY)
        assert stored is not None
        assert "s3cret-pass" not in stored
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        assert scheme == "scrypt"
        assert len(bytes.fromhex(salt_hex)) == 16
        assert int(n) >= 2**14

    def test_verify_without_set_is_false_not_crash(self, store):
        assert PassphraseVault(store).verify("anything") is False
