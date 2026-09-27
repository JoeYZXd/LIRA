"""设备端配置同步（U6，R30 设备侧）：WS 客户端 + 全量快照 (epoch, version) 原子幂等应用。

Key Decisions 落地（配置同步 = 全量快照 + (epoch, version) 键）：
  - WS 首帧出示 device token，未认证前不处理任何消息；
  - 快照先过协议严格解析 + 安全底线不变量校验（`lira.protocol`），
    再在单事务内原子应用；同 (epoch, version) 重复投递幂等跳过；
    应用后回执 SnapshotAck；
  - **epoch 只在重配对会话内换新**：设备端 `begin_pairing()` 生成一次性
    配对码（屏幕显示，子女在后台输入同一码）；配对会话外的任何陌生 epoch
    快照一律拒绝（旧 epoch 回放与陌生 epoch 注入同为此路径拒绝）；
  - 快照 `settings`（privacy_mode/tts_volume/tts_speed）随快照一并应用：
    privacy_mode 殊途同归触碰同一 `PrivacyState` 广播对象（与按键/UI 通道
    同源），音量/语速落注入的 `TtsSettings` 同形对象（SEC-3/F2）；
  - 心跳 60s 拉取兜底（`heartbeat_once`），安全关键变更靠后台在线立即推送；
  - WS 传输层可注入（`SyncTransport` 协议）：单元测试用 mock 传输，
    真实后台联通在 U9 验证，板上由 U10 挂真实传输。

协议消息 schema 单一来源在 `lira.protocol`（U8 后台复用同一模块）。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Awaitable, Callable, Protocol

from lira.appliances.ir import ApplianceError
from lira.appliances.models import ApplianceModel, SceneModel
from lira.appliances.store import ApplianceStore
from lira.privacy import PrivacyState
from lira.protocol import (
    AuthErrorMsg,
    AuthOkMsg,
    HelloMsg,
    LearnResultMsg,
    LearnStartMsg,
    ProtocolError,
    PullMsg,
    SnapshotAckMsg,
    SnapshotMsg,
    parse_frame,
)

__all__ = ["SyncError", "SyncTransport", "SyncClient", "SnapshotAppliedHook", "TtsSettings"]


class SyncError(Exception):
    """同步层致命错误（鉴权失败/传输关闭等），终止同步会话。"""


class SyncTransport(Protocol):
    """可注入的 WS 传输抽象（帧即 JSON-able dict）。"""

    async def send(self, frame: dict) -> None: ...

    async def receive(self) -> dict:
        """阻塞收一帧；连接关闭时抛 SyncError。"""

    async def close(self) -> None: ...


LearnHandler = Callable[[str, str], Awaitable[str]]

#: 快照应用后通知钩子（AE7）：(快照, 应用前各家电启用态) —— 供装配层对比
#: 出"本次推送新禁用了哪些家电"并口语播报；同步调用，异常由实现方自行消化。
SnapshotAppliedHook = Callable[[SnapshotMsg, dict[str, bool]], None]


class TtsSettings(Protocol):
    """音量/语速运行时设置对象（与 lira.ui.app.DeviceSettings 同形，鸭子类型解耦）。

    界定：越界值抛 ValueError（DeviceSettings 边界拒绝语义）；同步层按
    "单项设置失败不阻断快照"消化（见 `_apply_settings`）。
    """

    def set_volume(self, value: float) -> None: ...

    def set_tts_speed(self, value: float) -> None: ...


class SyncClient:
    """设备端同步会话。

    Args:
        store: 本地配置库（epoch/version/token 元数据在此）。
        transport: 可注入传输（mock 单测 / 真实 WS 由 U9/U10 接入）。
        token: device token（首帧鉴权）。
        learn_handler: 学习处理（device, action) -> code，通常为
            `LearningManager` 包装；注入 None 时忽略 learn_start 消息。
        privacy: 设备端隐私广播状态对象（SEC-3/F2 接入点）。注入后快照
            `settings.privacy_mode` 殊途同归触碰它（订阅者照常广播：关麦/
            封唤醒/后果播报）；注入 None 时忽略该设置。
        tts_settings: 音量/语速设置对象（DeviceSettings 同形）；注入 None
            时忽略 tts_volume/tts_speed。
    """

    #: R30：心跳拉取兜底周期
    HEARTBEAT_SECONDS = 60.0

    def __init__(
        self,
        *,
        store: ApplianceStore,
        transport: SyncTransport,
        token: str,
        learn_handler: LearnHandler | None = None,
        on_snapshot_applied: SnapshotAppliedHook | None = None,
        privacy: PrivacyState | None = None,
        tts_settings: TtsSettings | None = None,
    ) -> None:
        self._store = store
        self._transport = transport
        self._token = token
        self._learn_handler = learn_handler
        self._on_snapshot_applied = on_snapshot_applied
        self._privacy = privacy
        self._tts_settings = tts_settings
        self._authed = False
        self._pairing_code: str | None = None
        #: 拒绝记录（告警审计面，U7 状态卡/U9 断言用）
        self.rejections: list[str] = []

    # ---------- 配对（epoch 迁移唯一入口） ----------

    def begin_pairing(self) -> str:
        """开始重配对会话：生成一次性屏幕配对码（返回给调用方显示到屏幕）。

        会话内接受携带同码的新 epoch 快照；任何后续动作（成功/新会话）后失效。
        """
        self._pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        logging.info("配对会话已开启（配对码已生成，待屏幕显示）")
        return self._pairing_code

    @property
    def pairing_active(self) -> bool:
        return self._pairing_code is not None

    # ---------- 连接与鉴权 ----------

    async def connect(self) -> None:
        """建立会话：首帧出示 token；未认证前不处理任何业务消息。"""
        await self._transport.send(HelloMsg(token=self._token).to_json())
        frame = parse_frame(await self._transport.receive())
        if isinstance(frame, AuthOkMsg):
            self._authed = True
            logging.info("后台鉴权通过")
            return
        if isinstance(frame, AuthErrorMsg):
            raise SyncError(f"后台鉴权失败: {frame.reason}")
        raise SyncError(f"鉴权应答异常: {type(frame).__name__}")

    @property
    def authenticated(self) -> bool:
        return self._authed

    async def run_forever(self, *, heartbeat_seconds: float | None = None) -> None:
        """主循环：收帧处理 → 回执；周期心跳拉取兜底。连接关闭即返回。"""
        if not self._authed:
            await self.connect()
        period = self.HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
        while True:
            try:
                frame = await asyncio.wait_for(self._transport.receive(), timeout=period)
            except (asyncio.TimeoutError, TimeoutError):
                await self.heartbeat_once()
                continue
            reply = await self.handle_frame(frame)
            if reply is not None:
                await self._transport.send(reply)

    async def heartbeat_once(self) -> None:
        """心跳拉取兜底（R30）：上报当前 (epoch, version)，后台按需回快照。"""
        epoch = self._store.current_epoch() or 0
        version = self._store.current_version() or 0
        await self._transport.send(PullMsg(epoch=epoch, version=version).to_json())

    # ---------- 帧处理 ----------

    async def handle_frame(self, obj: dict) -> dict | None:
        """处理一帧已认证业务消息，返回应答帧（无需应答返回 None）。"""
        try:
            frame = parse_frame(obj)
        except ProtocolError as exc:
            self._reject(f"protocol_error: {exc}")
            return None
        if isinstance(frame, SnapshotMsg):
            return (await self._apply_snapshot(frame)).to_json()
        if isinstance(frame, LearnStartMsg):
            return await self._handle_learn(frame)
        logging.debug("忽略消息类型: %s", type(frame).__name__)
        return None

    async def _handle_learn(self, msg: LearnStartMsg) -> dict:
        if self._learn_handler is None:
            return LearnResultMsg(learn_id=msg.learn_id, error="learning_not_supported").to_json()
        try:
            code = await self._learn_handler(msg.device, msg.action)
        except ApplianceError as exc:
            return LearnResultMsg(learn_id=msg.learn_id, error=exc.kind).to_json()
        return LearnResultMsg(learn_id=msg.learn_id, code=code).to_json()

    # ---------- 快照应用（epoch 门禁 + 原子事务 + 幂等） ----------

    def _snapshot_models(self, snap: SnapshotMsg) -> list[ApplianceModel]:
        """DTO→模型。R32：码值是设备本地学习产物，快照不携带也**不得清空**——
        仍存在于配置中的设备/动作保留本地已学码值；被移除的动作随配置消失
        （store._upsert_appliance_tx 的"悬挂清理"语义）。"""
        local_codes: dict[tuple[str, str], str] = {
            (a.name, action): code
            for a in self._store.get_all_appliances()
            for action, code in a.codes.items()
        }
        models: list[ApplianceModel] = []
        for dto in snap.appliances:
            model = ApplianceModel.from_dto(dto)
            model.codes = {
                action: code for (name, action), code in local_codes.items()
                if name == model.name
            }
            models.append(model)
        return models

    async def _apply_snapshot(self, snap: SnapshotMsg) -> SnapshotAckMsg:
        current_epoch = self._store.current_epoch()
        current_version = self._store.current_version()

        if current_epoch is not None and snap.epoch != current_epoch:
            return await self._handle_foreign_epoch(snap, current_epoch)

        if current_epoch is not None and snap.epoch == current_epoch and current_version is not None:
            if snap.version <= current_version:
                # 幂等：同 (epoch, version) 重复投递直接跳过（不重复应用/播报）
                logging.debug("快照幂等跳过: epoch=%s version<=%s", snap.epoch, current_version)
                return SnapshotAckMsg(epoch=snap.epoch, version=snap.version,
                                      applied=False, reason="already_applied")

        # 首次配对（本地无 epoch）或同 epoch 的更新版本 → 原子应用。
        # device_token 只在配对会话内、且配对码核对一致时才落库（防同 epoch 帧偷换 token）。
        token_to_store: str | None = None
        if (
            self.pairing_active
            and snap.pairing_code is not None
            and secrets.compare_digest(snap.pairing_code, self._pairing_code)
        ):
            token_to_store = snap.device_token
        prev_enabled = self._prev_enabled()
        self._store.apply_snapshot(
            appliances=self._snapshot_models(snap),
            scenes=[SceneModel.from_dto(s) for s in snap.scenes],
            epoch=snap.epoch,
            version=snap.version,
            device_token=token_to_store,
        )
        await self._apply_settings(snap.settings)
        if token_to_store is not None:
            logging.info("配对完成：device token 已更新")
            self._pairing_code = None  # 一次性会话，用后即失效
        logging.info("快照已应用: epoch=%s version=%s appliances=%d scenes=%d",
                     snap.epoch, snap.version, len(snap.appliances), len(snap.scenes))
        self._notify_applied(snap, prev_enabled)
        return SnapshotAckMsg(epoch=snap.epoch, version=snap.version, applied=True)

    async def _handle_foreign_epoch(self, snap: SnapshotMsg, current_epoch: int) -> SnapshotAckMsg:
        """陌生/旧 epoch：仅配对会话内出示同码才接受（对抗旧快照回放）。"""
        if not self.pairing_active or snap.pairing_code is None:
            self._reject(
                f"epoch_mismatch: got epoch={snap.epoch} current={current_epoch}（拒绝应用，本地安全规则保持）"
            )
            return SnapshotAckMsg(epoch=snap.epoch, version=snap.version,
                                  applied=False, reason="epoch_mismatch")
        if not secrets.compare_digest(snap.pairing_code, self._pairing_code):
            self._reject("pairing_code_mismatch: 配对码不符（拒绝应用）")
            return SnapshotAckMsg(epoch=snap.epoch, version=snap.version,
                                  applied=False, reason="pairing_code_mismatch")
        # 通过门禁 → 走正常应用路径（此时 epoch 不等会被视为新纪元接受）
        prev_enabled = self._prev_enabled()
        self._store.apply_snapshot(
            appliances=self._snapshot_models(snap),
            scenes=[SceneModel.from_dto(s) for s in snap.scenes],
            epoch=snap.epoch,
            version=snap.version,
            device_token=snap.device_token,
        )
        await self._apply_settings(snap.settings)
        logging.info("重配对完成：epoch %s -> %s", current_epoch, snap.epoch)
        self._pairing_code = None
        self._notify_applied(snap, prev_enabled)
        return SnapshotAckMsg(epoch=snap.epoch, version=snap.version, applied=True)

    def _prev_enabled(self) -> dict[str, bool]:
        """应用前各家电启用态快照（供装配层对比出"新禁用名单"）。"""
        return {a.name: a.enabled for a in self._store.get_all_appliances()}

    def _notify_applied(self, snap: SnapshotMsg, prev_enabled: dict[str, bool]) -> None:
        """应用成功后通知装配层（hook 异常不得破坏同步主流程）。"""
        if self._on_snapshot_applied is None:
            return
        try:
            self._on_snapshot_applied(snap, prev_enabled)
        except Exception:  # noqa: BLE001 - 通知失败不回滚已应用的安全配置
            logging.exception("on_snapshot_applied 钩子异常（已忽略）")

    async def _apply_settings(self, settings: dict) -> None:
        """快照 `settings` 应用（SEC-3/F2 修复）：远程隐私开关此前被整段丢弃。

        - privacy_mode：殊途同归触碰同一 `PrivacyState` 广播对象（与物理按键/
          UI 通道同源；订阅者照常广播——关麦、封唤醒、后果播报；LLM 谓词每
          调用现查）。幂等由 `set_enabled` 保证：同值不广播、不留事件。
        - tts_volume/tts_speed：落注入的 `TtsSettings` 同形对象。

        纪律：单项设置应用失败（如越界）只告警，不回滚已原子入库的快照；
        设置值的运行时持久化与 DeviceSettings 现状一致，留给后续单元。
        """
        if self._privacy is not None and "privacy_mode" in settings:
            await self._privacy.set_enabled(bool(settings["privacy_mode"]), source="sync")
        if self._tts_settings is not None:
            for key, apply in (
                ("tts_volume", self._tts_settings.set_volume),
                ("tts_speed", self._tts_settings.set_tts_speed),
            ):
                if key not in settings:
                    continue
                try:
                    apply(float(settings[key]))
                except ValueError:
                    logging.warning("settings.%s 越界，已忽略（不阻断快照）", key)

    def _reject(self, reason: str) -> None:
        """拒绝即告警（R30/安全评审结论）：记入审计面 + WARNING 日志。"""
        self.rejections.append(reason)
        logging.warning("快照拒绝: %s", reason)

    # ---------- 便捷读取（状态机装配用） ----------

    def dialog_appliances(self) -> tuple:
        """当前本地库全部家电 → U3 意图层模型元组（Router 输入）。"""
        return tuple(a.to_dialog() for a in self._store.get_all_appliances())
