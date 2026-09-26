"""AE5：后台发起红外学习 → 遥控器按键 → 码值入库 → 语音复现（R32）。

真实后台（create_app + TestClient WS）+ 真实设备端 SyncClient + MockIr：
POST /admin/learn → WS 轮询下发 LearnStartMsg → 设备 learn() 录码（mock 队列）
→ LearnResultMsg 回传入库 + version+1 → 快照推送；随后语音"加大音量"命中
本地规则 → 第二道闸查码 → mock IR 复现。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SyncPump, WSAdapter, apost  # noqa: E402

from lira.sync import SyncClient  # noqa: E402

LEARNED_CODE = "P:9000,1200,3000,400"


def _learn_done(admin, learn_id: str) -> bool:
    body = admin.get(f"/admin/learn/{learn_id}").json()
    return body["status"] in ("done", "error")


async def test_ae5_backend_learn_then_voice_replay(harness, admin, device_token):
    h = harness
    # 后台配置一台电视机（音量加动作）
    assert apost(admin, "/admin/appliances", name="电视机", aliases="电视机,电视",
                 action="volume_up", triggers="加大音量,调大音量").status_code == 303

    with admin.websocket_connect("/ws/device") as ws:
        sync = SyncClient(
            store=h.store, transport=WSAdapter(ws), token=device_token,
            learn_handler=h.ir_service.learn,
            on_snapshot_applied=h.snapshot_announce_hook(),
        )
        pump = SyncPump(sync, ws)
        # 首连推送：配置入库（AE5 前置：家电在后台可见）
        await pump.run_until(
            lambda: h.store.get_appliance("电视机") is not None, timeout=5.0)
        assert h.store.current_epoch() == admin.app.state.db.epoch()

        # 后台发起学习（AE5 第 1 步）
        r = apost(admin, "/admin/learn", device="电视机", action="volume_up")
        assert r.status_code == 303
        learn_id = parse_qs(urlparse(r.headers["location"]).query)["learn_id"][0]

        # 遥控器按键（AE5 第 2 步）：mock 红外学习队列预置码值
        h.ir.learn_queue.append(LEARNED_CODE)

        # 泵持续收发直到学习回传入库（LearnStart→learn→LearnResult→done）
        await pump.run_until(lambda: _learn_done(admin, learn_id), timeout=10.0)
        body = admin.get(f"/admin/learn/{learn_id}").json()
        assert body["status"] == "done" and body["has_code"], body

        # 学习入库 version+1 → 快照推送 → 设备应用
        target_version = admin.app.state.db.version()
        await pump.run_until(
            lambda: h.store.current_version() == target_version, timeout=5.0)

    # 码值设备本地入库（R32：码值是学习产物，不经快照下发）
    assert h.store.get_appliance("电视机").codes.get("volume_up") == LEARNED_CODE

    # 语音复现（AE5 第 3 步）：本地规则命中（别名+触发词，G12）→ 非高危直发
    # → 第二道闸查码 → mock IR。（"调大音量"单说不含设备别名，产品语义不匹配）
    await h.engine.on_wake()
    await h.say_text("电视机调大音量")
    await h.settle(0.3)
    assert h.engine.state.name == "STANDBY"
    assert ("电视机", "volume_up") in h.ir_sent
    assert LEARNED_CODE in h.ir.sent_codes
    assert h.ir_errors == []


async def test_ae5_learn_via_real_voice(harness, admin, device_token):
    """同流程，复现一步走真实 wav（加大音量 ASR 可靠，见夹具验证记录）。"""
    h = harness
    if h.wake_stream is None or h.asr_stream is None:
        pytest.skip("KWS/ASR 模型未就绪（文本注入用例已覆盖逻辑）")
    assert apost(admin, "/admin/appliances", name="电视机", aliases="电视机,电视",
                 action="volume_up", triggers="加大音量,调大音量").status_code == 303
    h.ir.learn_queue.append(LEARNED_CODE)

    with admin.websocket_connect("/ws/device") as ws:
        sync = SyncClient(
            store=h.store, transport=WSAdapter(ws), token=device_token,
            learn_handler=h.ir_service.learn,
            on_snapshot_applied=h.snapshot_announce_hook(),
        )
        pump = SyncPump(sync, ws)
        await pump.run_until(
            lambda: h.store.get_appliance("电视机") is not None, timeout=5.0)
        r = apost(admin, "/admin/learn", device="电视机", action="volume_up")
        assert r.status_code == 303
        learn_id = parse_qs(urlparse(r.headers["location"]).query)["learn_id"][0]
        await pump.run_until(lambda: _learn_done(admin, learn_id), timeout=10.0)
        target_version = admin.app.state.db.version()
        await pump.run_until(
            lambda: h.store.current_version() == target_version, timeout=5.0)

    # 唤醒 + 指令全真声（wav 注入；"电视机调大音量"经真实 ASR 验证可靠）
    await h.say_wav("wake")
    assert h.engine.state.name == "LISTENING"
    await h.say_wav("volume_up_tv", tail_seconds=2.4)  # ASR 切句需 ≥2s 尾静音
    await h.settle(0.3)
    assert ("电视机", "volume_up") in h.ir_sent
