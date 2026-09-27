"""U8 API 与设备通信测试（计划 Test scenarios 全 8 条 + 补充缺口）。

设备角色全部在测试内模拟（计划：与真实后台的设备端联通属 U9）：
  - HTTP 通道：`X-Device-Token` 拉全量快照 / 回执；
  - WS 通道：TestClient websocket_connect，协议帧经 `app.protocol`
    （= device/lira/protocol.py 副本）严格解析；
  - 集成用例直接使用**真实的设备端** `lira.sync.SyncClient` +
    `lira.appliances.store.ApplianceStore`（sys.path 注入 device/），
    验证后台-设备协议两侧对齐。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import apost, extract_csrf, ws_device_session

from app import protocol
from app.protocol import PullMsg, SnapshotAckMsg, SnapshotMsg, parse_frame


# ---------- 协议复用纪律 ----------

def test_protocol_parity_with_device():
    """backend/app/protocol.py 必须与 device/lira/protocol.py 逐字节一致
    （schema 单一来源，System-Wide Impact「API surface parity」的防漂移闸）。"""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    device_src = (repo / "device/lira/protocol.py").read_text(encoding="utf-8")
    backend_src = (repo / "backend/app/protocol.py").read_text(encoding="utf-8")
    assert backend_src.endswith(device_src.split('"""', 2)[2])


# ---------- 场景 1（Happy path）：首启设置 → 登录 → 建设备 → token 拉全量快照 ----------

def test_first_boot_to_device_snapshot_pull(admin, client, device_token):
    # 管理员配置一台家电
    r = apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器,电暖器",
              action="打开", triggers="打开取暖器,开取暖器", is_high_risk="on")
    assert r.status_code == 303
    # 设备用 token 经 HTTP 拉全量快照
    r = client.get("/api/device/snapshot", headers={"X-Device-Token": device_token})
    assert r.status_code == 200
    snap = SnapshotMsg.from_json(r.json())  # 严格协议解析（含安全底线校验）
    assert snap.version == 1
    assert len(snap.appliances) == 1
    a = snap.appliances[0]
    assert (a.name, a.is_high_risk, a.enabled) == ("取暖器", True, True)
    assert a.actions == {"打开": ("打开取暖器", "开取暖器")}
    # WS 通道同样拉到一致快照
    with ws_device_session(client, device_token) as ws:
        ws.send_json(PullMsg(epoch=0, version=0).to_json())
        snap2 = SnapshotMsg.from_json(ws.receive_json())
    assert (snap2.epoch, snap2.version) == (snap.epoch, snap.version)


# ---------- 场景 2（Error path）：无/错 token 401（在 test_auth.py）+ WS 首帧纪律 ----------

def test_ws_first_frame_must_be_hello(client, admin, device_token):
    with client.websocket_connect("/ws/device") as ws:
        # 未出示 hello 就发业务帧：不处理，直接 auth_error
        ws.send_json(PullMsg(epoch=0, version=0).to_json())
        assert ws.receive_json()["type"] == "auth_error"
    with client.websocket_connect("/ws/device") as ws:
        ws.send_json({"type": "hello", "token": "wrong-token"})
        assert ws.receive_json() == {"type": "auth_error", "reason": "invalid_token"}


# ---------- 场景 3（AE7）：离线禁用高危设备 → 重连拉取 → 回执 ----------

def test_ae7_disable_offline_device_pull_and_ack(admin, client, device_token):
    apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器",
          action="打开", triggers="打开取暖器", is_high_risk="on")
    version_before = admin.app.state.db.version()

    # 设备离线时禁用（R7 安全关键变更）
    r = apost(admin, "/admin/appliances/取暖器/enabled", value="off")
    assert r.status_code == 303
    db = admin.app.state.db
    assert db.version() == version_before + 1
    assert db.get_appliance("取暖器")["enabled"] == 0

    # 设备重连经 HTTP 拉取 → 快照携带禁用态
    r = client.get("/api/device/snapshot", headers={"X-Device-Token": device_token})
    snap = SnapshotMsg.from_json(r.json())
    assert (snap.epoch, snap.version) == (db.epoch(), db.version())
    assert snap.appliances[0].enabled is False

    # 回执入库留痕
    r = client.post("/api/device/ack", headers={"X-Device-Token": device_token},
                    json=SnapshotAckMsg(epoch=snap.epoch, version=snap.version,
                                        applied=True).to_json())
    assert r.status_code == 200
    device = db.get_device("客厅设备")
    assert (device["ack_epoch"], device["ack_version"]) == (snap.epoch, snap.version)
    assert db.list_acks("客厅设备")[0]["applied"] == 1


def test_ae7_ws_online_push_within_poll_interval(admin, client, device_token):
    """安全关键变更在线时 WS 立即推送（R30 路径）。"""
    apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器",
          action="打开", triggers="打开取暖器")
    with ws_device_session(client, device_token) as ws:
        # 管理端禁用（WS 保持连接）
        r = apost(admin, "/admin/appliances/取暖器/enabled", value="off")
        assert r.status_code == 303
        # 设备在轮询周期内收到推送快照（无需主动 pull）
        snap = SnapshotMsg.from_json(ws.receive_json())
        assert snap.appliances[0].enabled is False
        # 回执
        ws.send_json(SnapshotAckMsg(epoch=snap.epoch, version=snap.version,
                                    applied=True).to_json())


# ---------- 场景 4（AE5）：发起学习 → 设备回传码值 → 入库且版本+1 ----------

def test_ae5_learn_flow_over_ws(admin, client, device_token):
    db = admin.app.state.db
    apost(admin, "/admin/appliances", name="空调", aliases="空调",
          action="打开", triggers="打开空调")
    version_before = db.version()

    with ws_device_session(client, device_token) as ws:
        # 声明已知当前版本（抑制连接初期的状态推送，使后续断言只见到学习完成推送）
        ws.send_json(PullMsg(epoch=db.epoch(), version=db.version()).to_json())
        # 管理端发起学习（异步下发：落库即返回）
        r = apost(admin, "/admin/learn", device="空调", action="打开")
        assert r.status_code == 303
        learn_id = parse_qs(urlparse(r.headers["location"]).query)["learn_id"][0]

        # 设备收到 learn_start（轮询周期内下发）
        frame = parse_frame(ws.receive_json())
        assert isinstance(frame, protocol.LearnStartMsg)
        assert (frame.learn_id, frame.device, frame.action) == (learn_id, "空调", "打开")

        # 设备回传码值（已认证 WS）
        code = "PULSE 9000 4500 560 1690"
        ws.send_json(protocol.LearnResultMsg(learn_id=learn_id, code=code).to_json())

        # 学习完成触发 version+1 → 推送新快照
        snap = SnapshotMsg.from_json(ws.receive_json())
        assert snap.version == version_before + 1

    # 码值入库（UPSERT）+ 状态面可查
    actions = {row["action"]: row for row in db.appliance_actions("空调")}
    assert actions["打开"]["code"] == code
    status = admin.get(f"/admin/learn/{learn_id}").json()
    assert status["status"] == "done" and status["has_code"] and status["error"] is None
    assert db.version() == version_before + 1


def test_learn_rejected_without_admin_auth(client, admin, device_token):
    r = client.post("/admin/learn", data={"device": "空调", "action": "打开"})
    assert r.status_code in (303, 401, 403)  # 未登录不可发起（重定向登录或拒绝）


def test_learn_error_result_recorded_without_version_bump(admin, client, device_token):
    db = admin.app.state.db
    apost(admin, "/admin/appliances", name="空调", aliases="空调",
          action="打开", triggers="打开空调")
    version_before = db.version()
    with ws_device_session(client, device_token) as ws:
        apost(admin, "/admin/learn", device="空调", action="打开")
        frame = parse_frame(ws.receive_json())
        ws.send_json(protocol.LearnResultMsg(learn_id=frame.learn_id,
                                             error="timeout").to_json())
        # 失败留痕但不 bump 版本 → 无新快照推送
    assert db.version() == version_before
    status = admin.get(f"/admin/learn/{frame.learn_id}").json()
    assert status["status"] == "done" and not status["has_code"]
    assert status["error"] == "timeout"


# ---------- 场景 4b（F4 竞态）：学习挂起期间家电被删除 → WS 连接不得崩溃 ----------

def test_learn_result_after_appliance_deleted_keeps_ws_alive(admin, client, device_token):
    """家电学习中途被删除：码值回传必须良性留痕（终态失败），
    IntegrityError 不得外泄杀死设备 WS 连接。"""
    db = admin.app.state.db
    apost(admin, "/admin/appliances", name="空调", aliases="空调",
          action="打开", triggers="打开空调")
    code = "PULSE 9000 4500 560 1690"

    with ws_device_session(client, device_token) as ws:
        # 声明已知当前版本（抑制连接初期的状态推送，使帧序确定）
        ws.send_json(PullMsg(epoch=db.epoch(), version=db.version()).to_json())
        r = apost(admin, "/admin/learn", device="空调", action="打开")
        assert r.status_code == 303
        learn_id = parse_qs(urlparse(r.headers["location"]).query)["learn_id"][0]
        frame = parse_frame(ws.receive_json())
        assert isinstance(frame, protocol.LearnStartMsg)

        # 学习挂起期间家电被后台删除（version+1）
        db.delete_appliance("空调")
        version_after_delete = db.version()

        # 设备回传码值：必须被良性处理，连接保持存活
        ws.send_json(protocol.LearnResultMsg(learn_id=learn_id, code=code).to_json())
        # 连接仍存活：删除触发的版本推送照常抵达（快照中已无该家电）
        snap = SnapshotMsg.from_json(ws.receive_json())
        assert all(a.name != "空调" for a in snap.appliances)

    # 学习记录为终态失败（码值无处入库），且未再抬升版本
    status = admin.get(f"/admin/learn/{learn_id}").json()
    assert status["status"] == "done" and not status["has_code"]
    assert status["error"] == "appliance_deleted"
    assert db.version() == version_after_delete


# ---------- 场景 5：旧 epoch 快照（后台库重建后）设备拒绝 —— 后台视角集成版 ----------

async def test_old_epoch_snapshot_rejected_by_device(
        admin, client, device_token, tmp_path):
    """后台 epoch 换新后，设备端（真实 SyncClient）拒绝新 epoch 快照并保持
    本地安全规则（U6 设备侧已测；此处验证后台-设备集成视角）。"""
    import secrets as _secrets

    from lira.appliances.models import ApplianceModel
    from lira.appliances.store import ApplianceStore
    from lira.sync import SyncClient

    from conftest import WSAdapter

    db = admin.app.state.db
    # 设备端本地库：与后台同 epoch 的旧配置（含禁用高危规则），落后一个版本
    store = ApplianceStore(tmp_path / "device.db")
    store.apply_snapshot(
        appliances=[ApplianceModel(name="取暖器", aliases=("取暖器",),
                                   actions={"打开": ("打开",)}, is_high_risk=True,
                                   enabled=False)],
        scenes=[], epoch=db.epoch(), version=0)
    old_state = (store.current_epoch(), store.current_version())

    with client.websocket_connect("/ws/device") as ws:
        adapter = WSAdapter(ws)
        device_client = SyncClient(store=store, transport=adapter, token=device_token)
        # SyncClient 自行完成 hello 首帧握手
        await device_client.connect()
        # 后台库重建（epoch 换新 + version 归零，与整库重建同语义）
        new_epoch = _secrets.randbits(31) + 1
        db.set_meta("epoch", str(new_epoch))
        db.set_meta("version", "0")
        # 设备心跳拉取 → 收到新 epoch 快照 → epoch 门禁拒绝
        await device_client.heartbeat_once()
        reply = None
        while reply is None:
            frame = parse_frame(await adapter.receive())
            assert isinstance(frame, SnapshotMsg)
            if frame.epoch == new_epoch:
                reply = await device_client.handle_frame(frame.to_json())
            else:  # 连接初期的同 epoch 推送，幂等跳过
                await device_client.handle_frame(frame.to_json())
        assert reply["applied"] is False and reply["reason"] == "epoch_mismatch"
        await adapter.send(reply)

    # 设备本地安全规则保持不变
    assert (store.current_epoch(), store.current_version()) == old_state
    assert store.get_appliance("取暖器").enabled is False
    assert any("epoch_mismatch" in r for r in device_client.rejections)
    # 回执已留痕（token 未轮换）
    acks = admin.app.state.db.list_acks("客厅设备")
    assert acks and acks[0]["applied"] == 0
    assert db.find_device_by_token(device_token) == "客厅设备"


# ---------- 场景 6：同版本重复投递幂等（后台视角：重复拉取字节一致） ----------

def test_same_version_repeated_pull_is_identical(admin, client, device_token):
    apost(admin, "/admin/appliances", name="灯", aliases="灯", action="打开",
          triggers="开灯")
    h1 = client.get("/api/device/snapshot", headers={"X-Device-Token": device_token})
    h2 = client.get("/api/device/snapshot", headers={"X-Device-Token": device_token})
    assert h1.json() == h2.json()
    snap = SnapshotMsg.from_json(h2.json())
    assert snap.version == 1


# ---------- 场景 7：SQLite WAL 下后台写与设备读并发不阻塞 ----------

def test_wal_concurrent_read_write_no_blocking(admin):
    db_path = str(admin.app.state.db._path)
    reader = sqlite3.connect(db_path)
    reader.execute("PRAGMA busy_timeout=2000")
    reader.execute("BEGIN")  # 打开读事务并持有
    version_before = reader.execute(
        "SELECT value FROM meta WHERE key='version'").fetchone()[0]

    # 后台并发写（管理端 API）不被读事务阻塞
    r = apost(admin, "/admin/settings/privacy", value="on")
    assert r.status_code == 303
    assert admin.app.state.db.version() == int(version_before) + 1

    # WAL 快照隔离：读者仍见旧版本，提交后可见新版本
    assert reader.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0] \
        == version_before
    reader.commit()
    reader.close()


# ---------- 场景 8（补充）：库重建后的开发期 epoch 迁移（reset-sync） ----------

async def test_backend_rebuild_epoch_migration_via_reset_sync(
        admin, client, device_token, tmp_path):
    """2026-09-27 决议：无配对流程。库重建 = epoch 换新 + version 归零 +
    旧 token 失效；开发期迁移路径：后台重新注册设备取新 token → 设备端
    reset-sync 清同步元数据 → bootstrap 接受新 epoch，已学码值保留。
    真实设备端 SyncClient 参与。"""
    import secrets as _secrets

    from lira.appliances.models import ApplianceModel
    from lira.appliances.store import ApplianceStore
    from lira.sync import SyncClient

    from conftest import WSAdapter

    db = admin.app.state.db
    # 后台先配置"灯"（否则新 epoch 快照不含该家电，设备端全量替换后学习成果无处保留）
    assert apost(admin, "/admin/appliances", name="灯", aliases="灯",
                 action="打开", triggers="开灯").status_code == 303
    store = ApplianceStore(tmp_path / "device.db")
    store.apply_snapshot(
        appliances=[ApplianceModel(name="灯", aliases=("灯",),
                                   actions={"打开": ("开灯",)}, enabled=True)],
        scenes=[], epoch=db.epoch(), version=3)
    store.upsert_code("灯", "打开", "learned-code-1")

    # ① 后台库重建（epoch 换新 + version 归零；设备表同样重建，等价于重新注册）
    new_epoch = _secrets.randbits(31) + 1
    db.set_meta("epoch", str(new_epoch))
    db.set_meta("version", "0")
    new_device_token = db.revoke_device_token("客厅设备")  # 重建后重新注册设备
    assert new_device_token != device_token

    # ② 设备端执行 `python -m lira.sync reset-sync`：仅清 (epoch, version)
    store.clear_sync_meta()

    # ③ 新 token 重连 → bootstrap 接受新 epoch 快照，已学码值保留
    with client.websocket_connect("/ws/device") as ws:
        adapter = WSAdapter(ws)
        device = SyncClient(store=store, transport=adapter, token=new_device_token)
        await device.connect()
        await device.heartbeat_once()
        reply = None
        while reply is None:
            frame = parse_frame(await adapter.receive())
            assert isinstance(frame, SnapshotMsg)
            if frame.epoch == new_epoch:
                reply = await device.handle_frame(frame.to_json())
            else:
                await device.handle_frame(frame.to_json())
        assert reply["applied"] is True
        await adapter.send(reply)

    assert store.current_epoch() == new_epoch
    assert store.current_version() == db.version()
    assert store.get_appliance("灯").codes["打开"] == "learned-code-1"

    # ④ 旧 token 已随重建失效；新 token 全链路可用
    assert db.find_device_by_token(device_token) is None
    r = client.get("/api/device/snapshot", headers={"X-Device-Token": new_device_token})
    snap = SnapshotMsg.from_json(r.json())
    assert snap.epoch == new_epoch
    assert [a.name for a in snap.appliances] == ["灯"]


# ---------- 补充：隐私远程开关进入快照 settings ----------

def test_privacy_remote_switch_flows_into_snapshot(admin, client, device_token):
    r = apost(admin, "/admin/settings/privacy", value="on")
    assert r.status_code == 303
    snap = SnapshotMsg.from_json(
        client.get("/api/device/snapshot",
                   headers={"X-Device-Token": device_token}).json())
    assert snap.settings["privacy_mode"] is True
    # 非法设置键被白名单拒绝（与协议 SETTINGS_ALLOWLIST 对齐）
    with pytest.raises(ValueError):
        admin.app.state.db.set_setting("skip_confirm", True)
