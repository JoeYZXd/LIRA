"""dev-console 观察链路跨层集成（dev-console 改版 U8，Verification Contract）。

计划分层表的"跨层集成"层：真实装配下 输入 → 同轮事件 → 鉴权快照——
    真实隐私门（PrivacyGatedSink）→ RouteDispatch（遥测旁路/投递）
      → DialogEngine（utterance→turn→operation 关联）
        → DevTelemetry 有界环 → /dev/api/observe 鉴权快照。

不覆盖"无第二麦克风流"子句（单测层已由观察者循环 mic_pause 断言钉住）；
身份/记忆隔离由 test_personal_memory / test_main_loop 承担。
"""

from __future__ import annotations

import numpy as np
import pytest

from harness import DeviceHarness  # noqa: F401 - conftest 提供 fixture 路径

from lira.appliances.store import ApplianceStore
from lira.dev_telemetry import DevTelemetry
from lira.privacy import PassphraseVault
from lira.settings import DeviceSettings
from lira.ui.dev import DevHandle
from lira.voice_log import VOICE_LOG

SAMPLE_RATE = 16000


@pytest.fixture()
def observed_harness(harness):
    """给 e2e 整机挂上 U2 遥测装配面（镜像 main.py 的生产接线）。"""
    h = harness
    h.telemetry = DevTelemetry()
    VOICE_LOG.register_sink(h.telemetry)
    h.privacy.subscribe(h.telemetry.apply_privacy)
    # U3 生产装配镜像：引擎层的 turn/route/speak_done 事件进同一遥测
    h.engine.telemetry = h.telemetry
    h.telemetry.add_observer()  # 包络计算开闸（生产由 observe 拉取续期）
    yield h
    VOICE_LOG.register_sink(None)
    VOICE_LOG.clear()


@pytest.fixture()
def dev_app(observed_harness):
    """真实 harness 之上的 dev 控制台 app。"""
    from lira.ui.app import UiServices, create_app

    h = observed_harness
    h.vault = PassphraseVault(h.store)
    h.settings = DeviceSettings()
    h.sync_status = {"session": "not_configured"}

    def _status() -> dict:
        """最小状态面（生产 DeviceRuntime.status 的 harness 等价物）。"""
        return {"engine": h.engine.state.value, "route": h.route.value,
                "privacy_on": h.privacy.is_on, "dnd": bool(h.engine.dnd),
                "volume": h.settings.volume, "network": False}

    h.status = _status  # DevHandle(h) 的鸭子类型运行时面：_status_payload 直调

    services = UiServices(
        privacy=h.privacy,
        vault=h.vault,
        store=h.store,
        settings=h.settings,
        status_provider=_status,
        dev=DevHandle(h),
    )
    h.vault.set("e2e-pass")
    return create_app(services)


async def test_observation_chain_wire_to_snapshot(observed_harness, dev_app):
    """输入 → 同轮事件 → 鉴权快照（计划 Verification Contract 跨层行）。"""
    from fastapi.testclient import TestClient

    h = observed_harness
    h.vault.set("e2e-pass")
    anonymous = TestClient(dev_app)
    # 未鉴权：快照不可读（原会话边界保护每一次请求）
    assert anonymous.get("/dev/api/observe").status_code == 401

    client = TestClient(dev_app)
    r = client.post("/dev/login", json={"passphrase": "e2e-pass"})
    assert r.status_code == 200, r.text

    # 真实输入：已知波形过真实隐私门 → dispatcher（遥测旁路在 feed 内）
    t = np.arange(SAMPLE_RATE, dtype=np.float32) / SAMPLE_RATE
    h._gate.feed((0.5 * np.sin(2 * np.pi * 50 * t)).astype(np.float32))

    # 真实投递：唤醒（on_wake 经回调 set_audio_route 已使 dispatcher 与引擎
    # 聆听代次自动对齐——生产唤醒的配对动作）→ 语句投递 → 状态机接受
    # → 本地规则路由 → 播报
    await h.engine.on_wake()
    h.dispatcher._deliver("帮助", wake_prefixed=False)  # 同步方法：内部 spawn 投递任务
    await h.drain()

    body = _observe(client)
    assert body["instance_id"] == h.telemetry.instance_id

    # 音频层（KTD2 跨层）：包络来自真实 gate.feed 的样本（非 0 电平假数据）
    envelopes = [f for f in body["audio"]["items"] if f["kind"] == "envelope"]
    assert envelopes and max(f["peak"] for f in envelopes) == pytest.approx(0.5, abs=0.01)

    # 事件层（KTD3 跨层）：utterance → turn → route(local_rule) → speak 关联链
    events = body["events"]["items"]
    utter = next(e for e in events if e["kind"] == "utterance")
    assert utter["text"] == "帮助" and utter["utterance_id"] is not None
    turn = next(e for e in events if e["kind"] == "turn")
    assert turn["turn_id"] is not None
    assert turn["utterance_id"] == utter["utterance_id"], "语句→轮次在同一快照内可追溯"
    route = next(e for e in events if e["kind"] == "route")
    assert route["text"] == "local_rule"
    assert route["turn_id"] == turn["turn_id"]
    assert any(e["kind"] in ("speak", "speak_done") for e in events), "播报事件入快照"

    # 增量：游标续读只回新增（协议跨层成立）
    cur = body["events"]["next_cursor"]
    again = _observe(client, event_cursor=cur)
    assert again["events"]["reset"] is False


async def test_privacy_blocks_snapshot_with_no_content(observed_harness, dev_app):
    """AE5 跨层：真实隐私订阅链翻转后，快照 403 且无任何 items。"""
    from fastapi.testclient import TestClient

    h = observed_harness
    client = TestClient(dev_app)
    client.post("/dev/login", json={"passphrase": "e2e-pass"})
    await h.privacy.set_enabled(True, source="e2e")
    r = client.get("/dev/api/observe")
    assert r.status_code == 403
    assert set(r.json().keys()) == {"error"}, "隐私拒绝不携带任何历史内容"


def _observe(client, *, event_cursor=None):
    params = {"audio_cursor": 0}
    if event_cursor is not None:
        params["event_cursor"] = event_cursor
    r = client.get("/dev/api/observe", params=params)
    assert r.status_code == 200, r.text
    return r.json()
