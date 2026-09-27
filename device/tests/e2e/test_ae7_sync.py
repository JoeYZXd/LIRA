"""AE7：离线期间后台禁用取暖器 → 设备重连拉取 → 应用并口语播报（R7/R30）。

真实后台 + 真实设备端 SyncClient + on_snapshot_applied 播报钩子：
禁用（安全关键变更）后台立即落库；设备离线错过的变更在重连首连推送 /
心跳拉取时补齐，"取暖器已被家人禁用" 只在 enabled→disabled 变化时播报一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SyncPump, WSAdapter, apost  # noqa: E402

from lira.sync import SyncClient  # noqa: E402


def _client(h, token, ws) -> SyncClient:
    return SyncClient(store=h.store, transport=WSAdapter(ws), token=token,
                      on_snapshot_applied=h.snapshot_announce_hook())


async def test_ae7_offline_disable_applied_and_announced_on_reconnect(
        harness, admin, device_token):
    h = harness
    # 后台配置高危取暖器并完成设备初始同步
    assert apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器,电暖器",
                 action="power", triggers="打开取暖器,开取暖器",
                 is_high_risk="on").status_code == 303

    with admin.websocket_connect("/ws/device") as ws:
        pump = SyncPump(_client(h, device_token, ws), ws)
        await pump.run_until(lambda: h.store.get_appliance("取暖器") is not None)
        assert h.store.get_appliance("取暖器").enabled is True
    # ---- 设备离线（WS 断开）----

    # 离线期间后台禁用（安全关键变更，后台立即落库 + version+1）
    assert apost(admin, "/admin/appliances/取暖器/enabled", value="off").status_code == 303
    disabled_version = admin.app.state.db.version()
    assert h.store.current_version() < disabled_version, "设备尚未知晓该变更"

    # 重连：首连推送补齐 → 应用 + 播报
    with admin.websocket_connect("/ws/device") as ws:
        pump = SyncPump(_client(h, device_token, ws), ws)
        await pump.run_until(
            lambda: h.store.current_version() == disabled_version)
        assert h.store.get_appliance("取暖器").enabled is False
        spoken = await h.wait_speech(lambda t: "已被家人禁用" in t)
        assert "取暖器" in spoken, f"播报应点名取暖器: {spoken}"

    # 禁用立即生效：语音操作被拒且不发 IR
    await h.engine.on_wake()
    await h.say_text("打开取暖器")
    await h.settle(0.2)
    assert h.ir_sent == [], "禁用设备不得发 IR"
    assert h.engine.state.name == "STANDBY"


async def test_ae7_heartbeat_pull_catches_up_after_offline(harness, admin, device_token):
    """心跳拉取兜底（R30）：设备主动上报落后版本 → 后台回最终快照。"""
    from lira.protocol import PullMsg

    h = harness
    assert apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器",
                 action="power", triggers="打开取暖器").status_code == 303
    with admin.websocket_connect("/ws/device") as ws:
        sync = _client(h, device_token, ws)
        pump = SyncPump(sync, ws)
        await pump.run_until(lambda: h.store.get_appliance("取暖器") is not None)
    v1 = h.store.current_version()

    # 离线变更（禁用）
    assert apost(admin, "/admin/appliances/取暖器/enabled", value="off").status_code == 303
    v2 = admin.app.state.db.version()

    with admin.websocket_connect("/ws/device") as ws:
        sync = _client(h, device_token, ws)
        pump = SyncPump(sync, ws)
        await sync.connect()
        await sync.heartbeat_once()  # 上报 (epoch, v1) → 落后 → 回快照
        await pump.run_until(lambda: h.store.current_version() == v2)
        assert h.store.get_appliance("取暖器").enabled is False


async def test_ae7_idempotent_push_no_duplicate_announce(harness, admin, device_token):
    """同 (epoch, version) 重复投递幂等跳过：不重复应用、不重复播报。"""
    h = harness
    assert apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器",
                 action="power", triggers="打开取暖器").status_code == 303
    with admin.websocket_connect("/ws/device") as ws:
        sync = _client(h, device_token, ws)
        pump = SyncPump(sync, ws)
        await pump.run_until(lambda: h.store.get_appliance("取暖器") is not None)
        assert apost(admin, "/admin/appliances/取暖器/enabled",
                     value="off").status_code == 303
        v = admin.app.state.db.version()
        await pump.run_until(lambda: h.store.current_version() == v)
        announce_count = sum(1 for t in h.speak_log if "已被家人禁用" in t)
        assert announce_count == 1

        # 同一连接内重复推送同版本快照（后台推:每 (epoch,version) 每连接一次，
        # 这里直接把缓存帧重放给设备端验证幂等）
        frame = admin.app.state.db.build_snapshot()
        # 重新走一遍同一快照（同 epoch/version）
        reply = await sync.handle_frame(frame.to_json())
        assert reply["applied"] is False and reply["reason"] == "already_applied"
        assert sum(1 for t in h.speak_log if "已被家人禁用" in t) == 1, \
            "幂等跳过不得重复播报"
