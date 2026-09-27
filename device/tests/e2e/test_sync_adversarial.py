"""同步对抗用例（R30 安全评审结论的 e2e 固化）：真实后台 + 真实设备端 SyncClient。

- 旧 epoch 快照回放：开发期 epoch 迁移（reset-sync）完成后再回放旧 epoch
  快照 → 拒绝 + 本地配置不被翻转；
- 同 (epoch, version) 重复投递：幂等跳过（不重复应用/播报）；
- 应用中崩溃：单事务原子性 → 本地库不变，重新拉取可干净应用；
- 后台库重建 → epoch 换新：运行期一律拒绝，reset-sync 清同步元数据后
  bootstrap 接受新 epoch 且已学码值保留（2026-09-27 决议：无配对流程）；
- 多次离线变更：设备只收/只应用最终快照一份。
"""

from __future__ import annotations

import secrets

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import SyncPump, WSAdapter, apost, ws_device_session  # noqa: E402

from lira.appliances.store import ApplianceStore  # noqa: E402
from lira.protocol import PullMsg  # noqa: E402
from lira.sync import SyncClient  # noqa: E402


def _client(h, token, ws, **kw) -> SyncClient:
    return SyncClient(store=h.store, transport=WSAdapter(ws), token=token,
                      on_snapshot_applied=h.snapshot_announce_hook(), **kw)


def _reconnect(h, sync: SyncClient, ws) -> SyncPump:
    """同一 SyncClient 换新连接（模拟设备重连：拒绝痕迹延续，token 不变）。"""
    sync._transport = WSAdapter(ws)
    sync._authed = False
    return SyncPump(sync, ws)


def _setup_heater(admin, *, high_risk: bool = False) -> None:
    assert apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器,电暖器",
                 action="power", triggers="打开取暖器,开取暖器",
                 **({"is_high_risk": "on"} if high_risk else {})).status_code == 303


async def _initial_sync(h, admin, device_token):
    """首连同步：取暖器配置入库；返回 (sync, pump)。"""
    with admin.websocket_connect("/ws/device") as ws:
        sync = _client(h, device_token, ws)
        pump = _reconnect(h, sync, ws)
        await pump.run_until(lambda: h.store.get_appliance("取暖器") is not None)
    return sync


# ---------- 1. 旧 epoch 回放 ----------


async def test_old_epoch_replay_rejected(harness, admin, device_token):
    h = harness
    _setup_heater(admin)
    sync = await _initial_sync(h, admin, device_token)

    # 捕获"旧纪元"快照（取暖器 enabled）
    with ws_device_session(admin, device_token) as ws:
        ws.send_json(PullMsg(epoch=0, version=0).to_json())
        old_snapshot = ws.receive_json()
    assert old_snapshot["type"] == "snapshot"
    old_epoch = old_snapshot["epoch"]

    # 后台库重建 → epoch 换新（epoch 换新 + version 归零，与整库重建同语义）
    db = admin.app.state.db
    db.set_meta("epoch", str(secrets.randbits(31) + 1))
    db.set_meta("version", "0")

    # 运行期新 epoch 快照一律拒绝，本地配置保持
    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        await sync.connect()
        rejections_before = len(sync.rejections)
        reply = await sync.handle_frame(db.build_snapshot().to_json())
        assert reply["applied"] is False and reply["reason"] == "epoch_mismatch"
        assert len(sync.rejections) == rejections_before + 1, "拒绝必须留痕（审计面）"
        assert h.store.current_epoch() == old_epoch, "本地 epoch 不得被翻改"

    # 开发期迁移：设备端 reset-sync 清同步元数据（保留配置与已学码值）
    h.store.clear_sync_meta()

    # 迁移后 bootstrap：接受新 epoch 快照
    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        await pump.run_until(
            lambda: h.store.current_epoch() == db.epoch() != old_epoch)
    assert h.store.get_appliance("取暖器").enabled is True, "新纪元初版仍启用"

    # 迁移完成后再回放旧 epoch 快照 → 必须拒绝，本地配置不被翻转
    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        await sync.connect()
        rejections_before = len(sync.rejections)
        reply = await sync.handle_frame(old_snapshot)
        assert reply["applied"] is False and reply["reason"] == "epoch_mismatch"
        assert len(sync.rejections) == rejections_before + 1, "拒绝必须留痕（审计面）"
        assert h.store.current_epoch() == db.epoch(), "本地 epoch 不得被回放翻改"
        assert h.store.get_appliance("取暖器").enabled is True


# ---------- 2. 同版本重复投递 ----------


async def test_same_version_duplicate_delivery_idempotent(harness, admin, device_token):
    h = harness
    _setup_heater(admin)
    with admin.websocket_connect("/ws/device") as ws:
        sync = _client(h, device_token, ws)
        pump = _reconnect(h, sync, ws)
        await pump.run_until(lambda: h.store.get_appliance("取暖器") is not None)
        assert apost(admin, "/admin/appliances/取暖器/enabled", value="off").status_code == 303
        v = admin.app.state.db.version()
        await pump.run_until(lambda: h.store.current_version() == v)
        assert sum(1 for t in h.speak_log if "已被家人禁用" in t) == 1

        # 直接重投同一快照帧（同 epoch/version）
        snap = admin.app.state.db.build_snapshot()
        reply = await sync.handle_frame(snap.to_json())
        assert reply["applied"] is False and reply["reason"] == "already_applied"
        assert sum(1 for t in h.speak_log if "已被家人禁用" in t) == 1, "不得重复播报"
        assert len(h.store.get_all_appliances()) == 1, "不得重复写入"


# ---------- 3. 应用中崩溃 → 原子回滚 → 重新拉取干净应用 ----------


async def test_mid_apply_crash_leaves_store_intact(harness, admin, device_token,
                                                   monkeypatch):
    h = harness
    _setup_heater(admin)
    assert apost(admin, "/admin/appliances", name="台灯", aliases="台灯",
                 action="power", triggers="打开台灯").status_code == 303
    sync = await _initial_sync(h, admin, device_token)
    v_applied = h.store.current_version()

    # 后台新增第三台（version+1）；设备端应用时在第二台入库处崩溃
    assert apost(admin, "/admin/appliances", name="风扇", aliases="风扇",
                 action="power", triggers="打开风扇").status_code == 303
    v_next = admin.app.state.db.version()
    assert v_next > v_applied

    orig_tx = ApplianceStore._upsert_appliance_tx
    calls = {"n": 0}

    def flaky_tx(self, model):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟应用中崩溃")
        return orig_tx(self, model)

    monkeypatch.setattr(ApplianceStore, "_upsert_appliance_tx", flaky_tx)

    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        with pytest.raises(RuntimeError, match="应用中崩溃"):
            await pump.run_until(lambda: h.store.current_version() == v_next)

    # 原子性：事务回滚，本地库仍是旧状态（2 台、旧版本）
    assert h.store.current_version() == v_applied
    assert {a.name for a in h.store.get_all_appliances()} == {"取暖器", "台灯"}

    # 修复后重新拉取 → 干净应用（3 台、新版本）
    monkeypatch.undo()
    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        await pump.run_until(lambda: h.store.current_version() == v_next)
        assert {a.name for a in h.store.get_all_appliances()} == \
            {"取暖器", "台灯", "风扇"}


# ---------- 4. 库重建 epoch 换新：运行期拒绝 / reset-sync 后接受 ----------


async def test_rebuilt_backend_epoch_rejected_until_reset_sync(harness, admin, device_token):
    h = harness
    _setup_heater(admin)
    sync = await _initial_sync(h, admin, device_token)
    old_epoch = h.store.current_epoch()
    h.store.upsert_code("取暖器", "power", "learned-code-1")  # 已学码值在场

    # 模拟后台库重建（epoch 换新 + version 归零，与整库重建同语义）
    db = admin.app.state.db
    db.set_meta("epoch", str(secrets.randbits(31) + 1))
    db.set_meta("version", "0")
    assert db.epoch() != old_epoch

    # 未迁移：新 epoch 快照一律拒绝，本地配置保持
    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        await sync.connect()
        reply = await sync.handle_frame(db.build_snapshot().to_json())
        assert reply["applied"] is False and reply["reason"] == "epoch_mismatch"
        assert sync.rejections, "陌生 epoch 必须留拒绝痕迹"
        assert h.store.current_epoch() == old_epoch, "本地 epoch 不得变"
        assert h.store.get_appliance("取暖器").enabled is True

    # 开发期迁移：reset-sync 仅清同步元数据
    h.store.clear_sync_meta()

    # 迁移后 bootstrap：接受新 epoch 快照，已学码值保留
    with admin.websocket_connect("/ws/device") as ws:
        pump = _reconnect(h, sync, ws)
        await pump.run_until(
            lambda: h.store.current_epoch() == db.epoch() and
            h.store.get_appliance("取暖器") is not None)
    assert h.store.get_appliance("取暖器").codes.get("power") == "learned-code-1"


# ---------- 5. 多次离线变更 → 只收最终快照 ----------


async def test_multiple_offline_changes_only_final_snapshot(harness, admin, device_token):
    h = harness
    _setup_heater(admin)
    await _initial_sync(h, admin, device_token)

    # 离线期间连续两次变更：禁用 → 再启用（最终态 = 启用）
    assert apost(admin, "/admin/appliances/取暖器/enabled", value="off").status_code == 303
    assert apost(admin, "/admin/appliances/取暖器/enabled", value="on").status_code == 303
    final_version = admin.app.state.db.version()

    with admin.websocket_connect("/ws/device") as ws:
        sync = _client(h, device_token, ws)
        pump = _reconnect(h, sync, ws)
        await pump.run_until(lambda: h.store.current_version() == final_version)
        # 设备只收到一份快照（首连推送一次，含最终态）
        assert pump.frames == 1, f"只应收到最终快照一帧: {pump.frames}"
        assert h.store.get_appliance("取暖器").enabled is True
        # 全程无"禁用播报"（最终态未发生 enabled→disabled 变化）
        assert not any("已被家人禁用" in t for t in h.speak_log)
        assert len(sync.rejections) == 0
