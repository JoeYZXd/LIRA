"""U6 设备端配置同步测试（计划 Test scenarios 后 3 条 + 补充缺口）。

覆盖：R30/AE7 快照原子应用+回执、同 (epoch,version) 幂等跳过、旧/陌生 epoch
一律拒绝且本地安全规则保持、开发期 epoch 迁移（clear_sync_meta 后 bootstrap
接受新 epoch 且已学码值保留，2026-09-27 决议：无配对流程）、违反安全底线快照
（未知字段/settings 白名单外/配对字段注入）拒绝并告警、WS 首帧 token 鉴权、
心跳拉取兜底、学习消息回传（R32）。

WS 传输层全部 mock 注入（计划：真实后台联通在 U9 验证）。
"""

from __future__ import annotations

import pytest

from lira.appliances.ir import ApplianceError, IRService
from lira.appliances.models import ApplianceModel
from lira.appliances.store import ApplianceStore
from lira.hal.mock.ir import MockIrController
from lira.privacy import PrivacyState
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


def snapshot_frame(*, epoch: int, version: int, enabled: bool = False) -> dict:
    """构造全量快照帧（关键点：禁用取暖器的安全规则在场）。"""
    return {
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
        """旧/陌生 epoch 快照 → 一律拒绝应用、本地安全规则保持。"""
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

    async def test_epoch_migration_via_reset_sync_cli(self, tmp_path):
        """2026-09-27 决议：无配对流程。epoch 迁移 = 开发期 reset-sync 清同步
        元数据（保留配置与已学码值）→ 重启后按 bootstrap 接受新 epoch。"""
        store = make_store(tmp_path)
        store.upsert_code("取暖器", "打开", "learned-code-9")  # 已学码值在场
        client = make_client(store, MockTransport())

        # reset-sync 前新 epoch 快照一律拒绝
        ack = await apply(client, snapshot_frame(epoch=9, version=1, enabled=True))
        assert ack["applied"] is False and ack["reason"] == "epoch_mismatch"

        # `python -m lira.sync reset-sync <db>` 语义：仅清 (epoch, version) 元数据
        store.clear_sync_meta()
        assert store.current_epoch() is None and store.current_version() is None

        # 重启后 bootstrap：接受新 epoch 快照，已学码值原样保留
        ack = await apply(client, snapshot_frame(epoch=9, version=1, enabled=True))
        assert ack["applied"] is True
        assert store.current_epoch() == 9
        assert store.get_appliance("取暖器").enabled is True
        assert store.get_appliance("取暖器").codes["打开"] == "learned-code-9"

    async def test_bootstrap_accepts_first_epoch(self, tmp_path):
        """出厂首次配置（本地无 epoch）：接受任意 epoch（bootstrap）。"""
        store = ApplianceStore(tmp_path / "fresh.db")
        client = make_client(store, MockTransport())
        ack = await apply(client, snapshot_frame(epoch=42, version=1))
        assert ack["applied"] is True
        assert store.current_epoch() == 42


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


# ---------- 快照 settings 应用（SEC-3/F2：远程隐私开关殊途同归） ----------


class FakeTtsSettings:
    """TtsSettings 同形替身：记录调用，可配置为越界拒绝（DeviceSettings 语义）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.reject: set[str] = set()

    def set_volume(self, value: float) -> None:
        if "volume" in self.reject:
            raise ValueError("音量须在 0.1~2.0 之间。")
        self.calls.append(("volume", value))

    def set_tts_speed(self, value: float) -> None:
        if "speed" in self.reject:
            raise ValueError("语速须在 0.5~2.0 之间。")
        self.calls.append(("speed", value))


def settings_frame(epoch: int, version: int, settings: dict) -> dict:
    frame = snapshot_frame(epoch=epoch, version=version)
    frame["settings"] = settings
    return frame


class TestSnapshotSettingsApply:
    async def test_privacy_on_engages_privacy_and_llm_gate(self, tmp_path):
        """SEC-3/F2 核心用例：远程推 privacy_mode=true → PrivacyState 开启。"""
        store = make_store(tmp_path)
        privacy = PrivacyState()
        client = make_client(store, MockTransport(), privacy=privacy)
        ack = await apply(client, settings_frame(1, 6, {"privacy_mode": True}))
        assert ack["applied"] is True
        assert privacy.is_on is True
        assert privacy() is True, "LLM 谓词注入点（fail-closed 门）应关闭上行"
        assert [e.source for e in privacy.events] == ["sync"], "与 UI 通道殊途同归"

    async def test_privacy_off_disengages(self, tmp_path):
        store = make_store(tmp_path)
        privacy = PrivacyState()
        client = make_client(store, MockTransport(), privacy=privacy)
        await privacy.set_enabled(True, source="ui")
        ack = await apply(client, settings_frame(1, 6, {"privacy_mode": False}))
        assert ack["applied"] is True
        assert privacy.is_on is False
        assert [e.source for e in privacy.events] == ["ui", "sync"]

    async def test_settings_apply_idempotent(self, tmp_path):
        """同值重复应用不重复广播；同 (epoch,version) 重投幂等跳过。"""
        store = make_store(tmp_path)
        privacy = PrivacyState()
        client = make_client(store, MockTransport(), privacy=privacy)
        await apply(client, settings_frame(1, 6, {"privacy_mode": True}))
        assert len(privacy.events) == 1
        # 同版本重投 → 快照整体幂等跳过
        ack = await apply(client, settings_frame(1, 6, {"privacy_mode": True}))
        assert ack["applied"] is False and ack["reason"] == "already_applied"
        # 更新版本携带同值 → 应用但不重复广播（PrivacyState 同值纪律）
        ack = await apply(client, settings_frame(1, 7, {"privacy_mode": True}))
        assert ack["applied"] is True
        assert len(privacy.events) == 1

    async def test_volume_and_speed_applied_to_settings_object(self, tmp_path):
        store = make_store(tmp_path)
        tts = FakeTtsSettings()
        client = make_client(store, MockTransport(), tts_settings=tts)
        ack = await apply(
            client, settings_frame(1, 6, {"privacy_mode": False, "tts_volume": 1.5, "tts_speed": 0.8})
        )
        assert ack["applied"] is True
        assert tts.calls == [("volume", 1.5), ("speed", 0.8)]

    async def test_out_of_range_volume_does_not_break_snapshot(self, tmp_path):
        """单项设置越界只告警，不回滚已原子入库的快照。"""
        store = make_store(tmp_path)
        tts = FakeTtsSettings()
        tts.reject.add("volume")
        client = make_client(store, MockTransport(), tts_settings=tts)
        ack = await apply(client, settings_frame(1, 6, {"tts_volume": 9.9, "tts_speed": 0.8}))
        assert ack["applied"] is True
        assert store.current_version() == 6  # 快照照常入库
        assert tts.calls == [("speed", 0.8)]  # 越界项被忽略，其余项生效

    async def test_no_privacy_injection_settings_ignored(self, tmp_path):
        """未注入 privacy/tts_settings（旧装配形态）→ settings 安全 no-op。"""
        store = make_store(tmp_path)
        client = make_client(store, MockTransport())
        ack = await apply(client, settings_frame(1, 6, {"privacy_mode": True}))
        assert ack["applied"] is True


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

    def test_pairing_fields_are_unknown_fields(self):
        """2026-09-27 决议：快照不携带 pairing_code/device_token——携带即未知字段拒绝。"""
        frame = snapshot_frame(epoch=1, version=1)
        frame["pairing_code"] = "123456"
        with pytest.raises(ProtocolError):
            SnapshotMsg.from_json(frame)
        frame = snapshot_frame(epoch=1, version=1)
        frame["device_token"] = "tok"
        with pytest.raises(ProtocolError):
            SnapshotMsg.from_json(frame)
