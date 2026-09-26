"""U6 设备端配置同步测试（计划 Test scenarios 后 3 条 + 补充缺口）。

覆盖：R30/AE7 快照原子应用+回执、同 (epoch,version) 幂等跳过、旧/陌生 epoch
拒绝且本地安全规则保持、重配对会话内新 epoch 接受并迁移（含 device token 轮换）、
配对码不符拒绝、违反安全底线快照（未知字段/settings 白名单外）拒绝并告警、
WS 首帧 token 鉴权、心跳拉取兜底、学习消息回传（R32）。

WS 传输层全部 mock 注入（计划：真实后台联通在 U9 验证）。
"""

from __future__ import annotations

import pytest

from lira.appliances.ir import ApplianceError, IRService
from lira.appliances.models import ApplianceModel
from lira.appliances.store import ApplianceStore
from lira.hal.mock.ir import MockIrController
from lira.protocol import ProtocolError, SnapshotMsg
from lira.sync import SyncClient, SyncError


class MockTransport:
    """内存传输：incoming 队列投喂，sent 记录全部出站帧。"""

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self.incoming: list[dict] = list(incoming or [])
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    async def receive(self) -> dict:
        if not self.incoming:
            raise SyncError("transport closed")
        return self.incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def make_store(tmp_path) -> ApplianceStore:
    store = ApplianceStore(tmp_path / "device.db")
    # 预置一份本地配置（含高危禁用规则），用于"拒绝后本地保持"断言
    store.apply_snapshot(
        appliances=[ApplianceModel(
            name="取暖器", aliases=("取暖器",), actions={"打开": ("打开",)},
            is_high_risk=True, enabled=False)],
        scenes=[],
        epoch=1,
        version=5,
    )
    return store


def make_client(store: ApplianceStore, transport: MockTransport, **kwargs) -> SyncClient:
    return SyncClient(store=store, transport=transport, token="dev-token-1", **kwargs)


def snapshot_frame(*, epoch: int, version: int, enabled: bool = False,
                   pairing_code: str | None = None, device_token: str | None = None) -> dict:
    """构造全量快照帧（关键点：禁用取暖器的安全规则在场）。"""
    obj = {
        "type": "snapshot",
        "epoch": epoch,
        "version": version,
        "appliances": [{
            "name": "取暖器",
            "aliases": ["取暖器"],
            "actions": {"打开": ["打开"]},
            "is_high_risk": True,
            "enabled": enabled,
        }],
        "scenes": [],
        "settings": {"privacy_mode": False},
    }
    if pairing_code is not None:
        obj["pairing_code"] = pairing_code
    if device_token is not None:
        obj["device_token"] = device_token
    return obj


async def apply(client: SyncClient, frame: dict) -> dict:
    return await client.handle_frame(frame)


# ---------- 握手鉴权 ----------


class TestAuth:
    async def test_hello_first_frame_and_auth_ok(self, tmp_path):
        store = make_store(tmp_path)
        transport = MockTransport(incoming=[{"type": "auth_ok"}])
        client = make_client(store, transport)
        assert client.authenticated is False
        await client.connect()
        assert client.authenticated is True
        assert transport.sent[0] == {"type": "hello", "token": "dev-token-1"}  # 首帧出示 token

    async def test_auth_error_raises(self, tmp_path):
        store = make_store(tmp_path)
        transport = MockTransport(incoming=[{"type": "auth_error", "reason": "bad token"}])
        client = make_client(store, transport)
        with pytest.raises(SyncError, match="bad token"):
            await client.connect()


# ---------- 快照应用（R30/AE7） ----------


class TestSnapshotApply:
    async def test_snapshot_applied_atomically_and_acked(self, tmp_path):
        """Covers R30/AE7: mock 传输注入全量快照 → 原子入库 → 回执 (epoch, version)。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        ack = await apply(client, snapshot_frame(epoch=1, version=6, enabled=True))
        assert ack == {"type": "snapshot_ack", "epoch": 1, "version": 6, "applied": True}
        assert store.current_epoch() == 1 and store.current_version() == 6
        assert store.get_appliance("取暖器").enabled is True  # 快照生效

    async def test_duplicate_version_idempotent_skip(self, tmp_path):
        """同 (epoch, version) 重复投递 → 幂等跳过、无重复应用。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        await apply(client, snapshot_frame(epoch=1, version=6, enabled=True))
        # 同版本但内容不同的恶意变体：不得被应用
        ack = await apply(client, snapshot_frame(epoch=1, version=6, enabled=False))
        assert ack["applied"] is False and ack["reason"] == "already_applied"
        assert store.get_appliance("取暖器").enabled is True  # 内容未被覆盖
        # 更旧版本同样跳过
        ack = await apply(client, snapshot_frame(epoch=1, version=2, enabled=False))
        assert ack["applied"] is False
        assert store.get_appliance("取暖器").enabled is True

    async def test_same_epoch_newer_version_applies(self, tmp_path):
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        await apply(client, snapshot_frame(epoch=1, version=6, enabled=True))
        ack = await apply(client, snapshot_frame(epoch=1, version=7, enabled=False))
        assert ack["applied"] is True
        assert store.get_appliance("取暖器").enabled is False


# ---------- epoch 门禁（对抗性） ----------


class TestEpochGates:
    async def test_old_or_foreign_epoch_rejected_local_rules_kept(self, tmp_path):
        """旧/陌生 epoch 快照（配对会话外）→ 拒绝应用、本地安全规则保持。"""
        store = make_store(tmp_path)  # 本地: epoch=1 version=5, 取暖器 disabled
        client = make_client(store, MockTransport())
        # 陌生 epoch（后台重建后的"新"纪元，未走重配对）
        ack = await apply(client, snapshot_frame(epoch=9, version=1, enabled=True))
        assert ack["applied"] is False and ack["reason"] == "epoch_mismatch"
        # 旧数据回流（epoch 回退同样陌生）
        ack = await apply(client, snapshot_frame(epoch=0, version=99, enabled=True))
        assert ack["applied"] is False
        # 本地安全规则原封不动
        assert store.current_epoch() == 1
        appliance = store.get_appliance("取暖器")
        assert appliance.enabled is False and appliance.is_high_risk is True
        # 拒绝即告警（审计面）
        assert any("epoch_mismatch" in r for r in client.rejections)

    async def test_pairing_session_accepts_new_epoch_and_migrates(self, tmp_path):
        """配对会话内出示新 epoch + 屏幕配对码 → 接受并落库（epoch 迁移）。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        code = client.begin_pairing()
        assert client.pairing_active is True
        assert len(code) == 6 and code.isdigit()  # 屏显一次性配对码
        ack = await apply(client, snapshot_frame(
            epoch=9, version=1, enabled=True, pairing_code=code, device_token="new-token"))
        assert ack["applied"] is True
        assert store.current_epoch() == 9
        assert store.device_token() == "new-token"  # 新 token 落库
        assert store.get_appliance("取暖器").enabled is True
        assert client.pairing_active is False  # 一次性会话用后即失效

    async def test_wrong_pairing_code_rejected(self, tmp_path):
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        client.begin_pairing()
        ack = await apply(client, snapshot_frame(
            epoch=9, version=1, enabled=True, pairing_code="000000"))
        assert ack["applied"] is False and ack["reason"] == "pairing_code_mismatch"
        assert store.current_epoch() == 1  # 本地保持
        assert client.pairing_active is True  # 会话仍在，可重试

    async def test_bootstrap_accepts_first_epoch(self, tmp_path):
        """出厂首次配置（本地无 epoch）：接受任意 epoch（bootstrap）。"""
        store = ApplianceStore(tmp_path / "fresh.db")
        client = make_client(store, MockTransport())
        ack = await apply(client, snapshot_frame(epoch=42, version=1))
        assert ack["applied"] is True
        assert store.current_epoch() == 42

    async def test_pairing_token_not_accepted_without_matching_code(self, tmp_path):
        """同 epoch 快照借配对会话偷换 token → token 不落库。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        client.begin_pairing()
        await apply(client, snapshot_frame(epoch=1, version=6, device_token="evil"))
        assert store.device_token() is None


# ---------- 安全底线不变量 ----------


class TestSafetyFloor:
    async def test_snapshot_with_unknown_appliance_field_rejected(self, tmp_path):
        """试图注入"免确认"字段（高危设备 skip_confirm）→ 解析期拒绝 + 告警。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        frame = snapshot_frame(epoch=1, version=6)
        frame["appliances"][0]["skip_confirm"] = True  # schema 中不存在的字段
        reply = await apply(client, frame)
        assert reply is None  # 拒绝应答
        assert any("protocol_error" in r for r in client.rejections)
        # 本地配置未被触碰
        assert store.current_version() == 5

    async def test_snapshot_with_unknown_settings_key_rejected(self, tmp_path):
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        frame = snapshot_frame(epoch=1, version=6)
        frame["settings"] = {"bypass_confirmation": True}  # 白名单外设置键
        await apply(client, frame)
        assert any("protocol_error" in r for r in client.rejections)
        assert store.current_version() == 5

    async def test_snapshot_with_codes_field_rejected(self, tmp_path):
        """码值是设备本地学习产物，快照携带 codes 字段即非法帧。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        frame = snapshot_frame(epoch=1, version=6)
        frame["appliances"][0]["codes"] = {"打开": "forged"}
        await apply(client, frame)
        assert any("protocol_error" in r for r in client.rejections)

    async def test_malformed_frame_ignored(self, tmp_path):
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        assert await apply(client, {"type": "snapshot", "epoch": "x"}) is None
        assert await apply(client, {"type": "unknown_type"}) is None


# ---------- 心跳与学习 ----------


class TestHeartbeatAndLearn:
    async def test_heartbeat_pulls_with_current_epoch_version(self, tmp_path):
        store = make_store(tmp_path)
        transport = MockTransport()
        client = make_client(store, transport)
        await client.heartbeat_once()
        assert transport.sent == [{"type": "pull", "epoch": 1, "version": 5}]

    async def test_heartbeat_reply_snapshot_applied(self, tmp_path):
        """心跳之后注入的快照与推送路径共用同一应用逻辑。"""
        store = make_store(tmp_path)
        transport = MockTransport()
        client = make_client(store, transport)
        await client.heartbeat_once()
        ack = await apply(client, snapshot_frame(epoch=1, version=6))
        assert ack["applied"] is True
        assert store.current_version() == 6

    async def test_learn_start_returns_code_and_stores(self, tmp_path):
        """Covers R32: 后台下发学习 → 设备录码 → 入库 + 回传（已认证 WS）。

        learn_handler 即真实装配形态：IRService.learn（HAL 录码 + 本地入库）。
        """
        store = make_store(tmp_path)
        ir = MockIrController()
        ir.learn_queue = ["learned-code-1"]
        service = IRService(store, ir)

        async def learn_handler(device: str, action: str) -> str:
            return await service.learn(device, action)

        client = make_client(store, MockTransport(), learn_handler=learn_handler)
        reply = await apply(client, {"type": "learn_start", "learn_id": "L1",
                                     "device": "取暖器", "action": "打开"})
        assert reply == {"type": "learn_result", "learn_id": "L1", "code": "learned-code-1"}
        assert store.get_appliance("取暖器").codes["打开"] == "learned-code-1"

    async def test_learn_failure_reported(self, tmp_path):
        async def learn_handler(device: str, action: str) -> str:
            raise ApplianceError("learn_failed", "超时")

        client = make_client(make_store(tmp_path), MockTransport(), learn_handler=learn_handler)
        reply = await apply(client, {"type": "learn_start", "learn_id": "L2",
                                     "device": "电视", "action": "打开"})
        assert reply == {"type": "learn_result", "learn_id": "L2", "error": "learn_failed"}

    async def test_learn_without_handler_reports_unsupported(self, tmp_path):
        client = make_client(make_store(tmp_path), MockTransport(), learn_handler=None)
        reply = await apply(client, {"type": "learn_start", "learn_id": "L3",
                                     "device": "电视", "action": "打开"})
        assert reply == {"type": "learn_result", "learn_id": "L3", "error": "learning_not_supported"}


# ---------- 协议层直测（U8 对齐用例） ----------


class TestProtocolDirect:
    def test_snapshot_roundtrip(self):
        msg = SnapshotMsg.from_json(snapshot_frame(epoch=3, version=4))
        assert msg.epoch == 3 and msg.version == 4
        assert msg.appliances[0].name == "取暖器"
        assert msg.to_json()["type"] == "snapshot"

    def test_duplicate_appliance_names_rejected(self):
        frame = snapshot_frame(epoch=1, version=1)
        frame["appliances"].append(dict(frame["appliances"][0]))
        with pytest.raises(ProtocolError):
            SnapshotMsg.from_json(frame)

    def test_pairing_code_type_checked(self):
        frame = snapshot_frame(epoch=1, version=1, pairing_code=123)
        with pytest.raises(ProtocolError):
            SnapshotMsg.from_json(frame)
