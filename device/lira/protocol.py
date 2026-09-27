"""LIRA 设备↔后台 WS 协议：消息 schema 的**单一来源**（U6/U8 共用）。

System-Wide Impact「API surface parity」要求设备侧（lira.sync）与后台侧
（backend，U8）共用同一 schema 定义，避免双份漂移。U8 复用方式：
把 `device/` 包作为可安装依赖（`pip install -e device`）后
`from lira.protocol import ...`；若后台不引 Python 包，则复制本文件并
以本文件的 docstring 为对齐基准（字段名即协议）。

安全底线（safety floor）在本 schema 中的结构性表达：
  - 高危二次确认逻辑是**代码常量**（R6/R23），协议中**不存在**任何
    "免确认/跳过确认"字段；`ApplianceDTO.from_json` 严格拒绝未知字段，
    任何试图注入 `skip_confirm` 之类字段的快照在解析期即被拒绝。
  - `settings` 键必须在 `SETTINGS_ALLOWLIST` 白名单内（隐私模式、音量、
    语速），任何未知设置键同样是解析期拒绝。
  - 解析成功 ≠ 可应用：epoch/version 门禁与原子事务在 `lira.sync`。

线路格式：JSON 对象，`type` 字段判别消息类型。所有消息字段定长定型，
解析一律 fail-closed（未知字段/错误类型 → ProtocolError）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "ProtocolError",
    "SETTINGS_ALLOWLIST",
    "ApplianceDTO",
    "SceneStepDTO",
    "SceneDTO",
    "SnapshotMsg",
    "HelloMsg",
    "AuthOkMsg",
    "AuthErrorMsg",
    "SnapshotAckMsg",
    "PullMsg",
    "LearnStartMsg",
    "LearnResultMsg",
    "parse_frame",
    "validate_snapshot",
]


class ProtocolError(Exception):
    """协议帧非法（未知字段/类型错误/缺必填项）。调用方必须拒绝该帧。"""


# settings 白名单：{键: 期望类型}。类型校验用严格 isinstance（bool 不是 int）。
SETTINGS_ALLOWLIST: dict[str, type] = {
    "privacy_mode": bool,
    "tts_volume": float,
    "tts_speed": float,
}


# ---------- 严格字段校验辅助（全部 fail-closed） ----------


def _require_mapping(obj: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ProtocolError(f"{where} 应为对象，实际为 {type(obj).__name__}")
    return obj


def _reject_unknown(obj: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise ProtocolError(f"{where} 含未知字段: {sorted(unknown)}（安全底线：未知字段一律拒绝）")


def _require_keys(obj: Mapping[str, Any], keys: set[str], where: str) -> None:
    missing = keys - set(obj)
    if missing:
        raise ProtocolError(f"{where} 缺少必填字段: {sorted(missing)}")


def _get_str(obj: Mapping[str, Any], key: str, where: str) -> str:
    v = obj[key]
    if not isinstance(v, str) or not v:
        raise ProtocolError(f"{where}.{key} 应为非空字符串")
    return v


def _get_bool(obj: Mapping[str, Any], key: str, where: str) -> bool:
    v = obj[key]
    if not isinstance(v, bool):
        raise ProtocolError(f"{where}.{key} 应为布尔值")
    return v


def _get_int(obj: Mapping[str, Any], key: str, where: str) -> int:
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError(f"{where}.{key} 应为整数")
    return v


def _get_str_tuple(obj: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    v = obj[key]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ProtocolError(f"{where}.{key} 应为非空字符串数组")
    return tuple(v)


# ---------- 配置实体 DTO ----------


@dataclass(frozen=True)
class ApplianceDTO:
    """家电条目（快照内）。字段名即协议，与 lira.appliances.models 对齐。"""

    name: str
    aliases: tuple[str, ...]
    actions: dict[str, tuple[str, ...]]
    is_high_risk: bool = False
    enabled: bool = True

    ALLOWED_KEYS = {"name", "aliases", "actions", "is_high_risk", "enabled"}

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "actions": {k: list(v) for k, v in self.actions.items()},
            "is_high_risk": self.is_high_risk,
            "enabled": self.enabled,
        }

    @classmethod
    def from_json(cls, obj: Any) -> "ApplianceDTO":
        where = "appliance"
        o = _require_mapping(obj, where)
        # 注意：码值（pulse/space 原始码）是设备本地学习产物，不经快照下发，
        # 因此本 DTO 没有 code 字段——快照中出现 "codes" 键同样是未知字段拒绝。
        _reject_unknown(o, cls.ALLOWED_KEYS, where)
        _require_keys(o, {"name", "aliases", "actions"}, where)
        actions_raw = _require_mapping(o["actions"], f"{where}.actions")
        actions: dict[str, tuple[str, ...]] = {}
        for action, triggers in actions_raw.items():
            if not isinstance(action, str) or not action:
                raise ProtocolError(f"{where}.actions 键应为非空字符串")
            actions[action] = _get_str_tuple({"v": triggers}, "v", f"{where}.actions.{action}")
        return cls(
            name=_get_str(o, "name", where),
            aliases=_get_str_tuple(o, "aliases", where),
            actions=actions,
            is_high_risk=_get_bool(o, "is_high_risk", where) if "is_high_risk" in o else False,
            enabled=_get_bool(o, "enabled", where) if "enabled" in o else True,
        )


@dataclass(frozen=True)
class SceneStepDTO:
    device: str
    action: str

    def to_json(self) -> dict[str, str]:
        return {"device": self.device, "action": self.action}


@dataclass(frozen=True)
class SceneDTO:
    """场景 = 设备动作列表（Key Decisions「整状态帧」：动作名即空调目标状态）。"""

    name: str
    steps: tuple[SceneStepDTO, ...]

    ALLOWED_KEYS = {"name", "steps"}

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "steps": [s.to_json() for s in self.steps]}

    @classmethod
    def from_json(cls, obj: Any) -> "SceneDTO":
        where = "scene"
        o = _require_mapping(obj, where)
        _reject_unknown(o, cls.ALLOWED_KEYS, where)
        _require_keys(o, {"name", "steps"}, where)
        steps_raw = o["steps"]
        if not isinstance(steps_raw, list):
            raise ProtocolError(f"{where}.steps 应为数组")
        steps: list[SceneStepDTO] = []
        for i, raw_step in enumerate(steps_raw):
            step_where = f"{where}.steps[{i}]"
            so = _require_mapping(raw_step, step_where)
            _reject_unknown(so, {"device", "action"}, step_where)
            _require_keys(so, {"device", "action"}, step_where)
            steps.append(
                SceneStepDTO(
                    device=_get_str(so, "device", step_where),
                    action=_get_str(so, "action", step_where),
                )
            )
        return cls(name=_get_str(o, "name", where), steps=tuple(steps))


# ---------- 配置同步消息 ----------


@dataclass(frozen=True)
class SnapshotMsg:
    """后台 → 设备：全量配置快照（Key Decisions：(epoch, version) 键）。

    2026-09-27 决议：无配对流程——快照不携带 pairing_code / device_token，
    携带即未知字段拒绝；device token 由设备端配置预置（开发期写入）。
    """

    epoch: int
    version: int
    appliances: tuple[ApplianceDTO, ...]
    scenes: tuple[SceneDTO, ...]
    settings: dict[str, Any]

    TYPE = "snapshot"
    ALLOWED_KEYS = {
        "type",
        "epoch",
        "version",
        "appliances",
        "scenes",
        "settings",
    }

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "epoch": self.epoch,
            "version": self.version,
            "appliances": [a.to_json() for a in self.appliances],
            "scenes": [s.to_json() for s in self.scenes],
            "settings": dict(self.settings),
        }

    @classmethod
    def from_json(cls, obj: Any) -> "SnapshotMsg":
        where = cls.TYPE
        o = _require_mapping(obj, where)
        _reject_unknown(o, cls.ALLOWED_KEYS, where)
        _require_keys(o, {"epoch", "version", "appliances", "scenes", "settings"}, where)
        appliances_raw = o["appliances"]
        scenes_raw = o["scenes"]
        if not isinstance(appliances_raw, list) or not isinstance(scenes_raw, list):
            raise ProtocolError(f"{where}.appliances / {where}.scenes 应为数组")
        snapshot = cls(
            epoch=_get_int(o, "epoch", where),
            version=_get_int(o, "version", where),
            appliances=tuple(ApplianceDTO.from_json(a) for a in appliances_raw),
            scenes=tuple(SceneDTO.from_json(s) for s in scenes_raw),
            settings=_require_mapping(o["settings"], f"{where}.settings"),
        )
        validate_snapshot(snapshot)
        return snapshot


def validate_snapshot(snapshot: SnapshotMsg) -> None:
    """安全底线不变量校验（设备应用快照前的最后一道解析级闸）。

    - settings 键必须在白名单内且类型严格正确；
    - 家电名称不得重复（PK 冲突即配置非法）。
    高危"免确认"路径不存在于 schema（结构上不可表达），未知字段在
    from_json 阶段已被拒绝——本函数是白名单与一致性的显式复核。
    """
    for key, value in snapshot.settings.items():
        expected = SETTINGS_ALLOWLIST.get(key)
        if expected is None:
            raise ProtocolError(
                f"settings 含未知键 {key!r}（安全底线：设置项白名单外的键一律拒绝）"
            )
        if expected is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProtocolError(f"settings.{key} 应为数字")
        elif not isinstance(value, expected):
            raise ProtocolError(f"settings.{key} 类型错误，期望 {expected.__name__}")
    names = [a.name for a in snapshot.appliances]
    if len(names) != len(set(names)):
        raise ProtocolError("快照中存在重名家电商条目（一致性校验失败）")
    scene_names = [s.name for s in snapshot.scenes]
    if len(scene_names) != len(set(scene_names)):
        raise ProtocolError("快照中存在重名场景（一致性校验失败）")


# ---------- 握手 / 回执 / 学习消息 ----------


@dataclass(frozen=True)
class HelloMsg:
    """设备 → 后台：WS 连接后的**首帧**，出示 device token（未认证前后台不处理任何消息）。"""

    token: str

    TYPE = "hello"
    ALLOWED_KEYS = {"type", "token"}

    def to_json(self) -> dict[str, Any]:
        return {"type": self.TYPE, "token": self.token}

    @classmethod
    def from_json(cls, obj: Any) -> "HelloMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        _require_keys(o, {"token"}, cls.TYPE)
        return cls(token=_get_str(o, "token", cls.TYPE))


@dataclass(frozen=True)
class AuthOkMsg:
    TYPE = "auth_ok"
    ALLOWED_KEYS = {"type"}

    def to_json(self) -> dict[str, Any]:
        return {"type": self.TYPE}

    @classmethod
    def from_json(cls, obj: Any) -> "AuthOkMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        return cls()


@dataclass(frozen=True)
class AuthErrorMsg:
    reason: str

    TYPE = "auth_error"
    ALLOWED_KEYS = {"type", "reason"}

    def to_json(self) -> dict[str, Any]:
        return {"type": self.TYPE, "reason": self.reason}

    @classmethod
    def from_json(cls, obj: Any) -> "AuthErrorMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        _require_keys(o, {"reason"}, cls.TYPE)
        return cls(reason=_get_str(o, "reason", cls.TYPE))


@dataclass(frozen=True)
class SnapshotAckMsg:
    """设备 → 后台：快照应用回执。applied=False 时 reason 说明跳过/拒绝原因。"""

    epoch: int
    version: int
    applied: bool
    reason: str | None = None

    TYPE = "snapshot_ack"
    ALLOWED_KEYS = {"type", "epoch", "version", "applied", "reason"}

    def to_json(self) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "type": self.TYPE,
            "epoch": self.epoch,
            "version": self.version,
            "applied": self.applied,
        }
        if self.reason is not None:
            obj["reason"] = self.reason
        return obj

    @classmethod
    def from_json(cls, obj: Any) -> "SnapshotAckMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        _require_keys(o, {"epoch", "version", "applied"}, cls.TYPE)
        ack = cls(
            epoch=_get_int(o, "epoch", cls.TYPE),
            version=_get_int(o, "version", cls.TYPE),
            applied=_get_bool(o, "applied", cls.TYPE),
        )
        reason = o.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ProtocolError(f"{cls.TYPE}.reason 应为字符串")
        object.__setattr__(ack, "reason", reason)
        return ack


@dataclass(frozen=True)
class PullMsg:
    """设备 → 后台：心跳拉取兜底（60s，R30）。携带设备当前 (epoch, version)。"""

    epoch: int
    version: int

    TYPE = "pull"
    ALLOWED_KEYS = {"type", "epoch", "version"}

    def to_json(self) -> dict[str, Any]:
        return {"type": self.TYPE, "epoch": self.epoch, "version": self.version}

    @classmethod
    def from_json(cls, obj: Any) -> "PullMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        _require_keys(o, {"epoch", "version"}, cls.TYPE)
        return cls(epoch=_get_int(o, "epoch", cls.TYPE), version=_get_int(o, "version", cls.TYPE))


@dataclass(frozen=True)
class LearnStartMsg:
    """后台 → 设备：发起红外学习（R32）。设备进入学习模式录一帧原始码。"""

    learn_id: str
    device: str
    action: str

    TYPE = "learn_start"
    ALLOWED_KEYS = {"type", "learn_id", "device", "action"}

    def to_json(self) -> dict[str, Any]:
        return {"type": self.TYPE, "learn_id": self.learn_id, "device": self.device, "action": self.action}

    @classmethod
    def from_json(cls, obj: Any) -> "LearnStartMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        _require_keys(o, {"learn_id", "device", "action"}, cls.TYPE)
        return cls(
            learn_id=_get_str(o, "learn_id", cls.TYPE),
            device=_get_str(o, "device", cls.TYPE),
            action=_get_str(o, "action", cls.TYPE),
        )


@dataclass(frozen=True)
class LearnResultMsg:
    """设备 → 后台：学习结果回传（已认证 WS）。code 为 ir-ctl 原始 pulse/space 文本。"""

    learn_id: str
    code: str | None = None
    error: str | None = None

    TYPE = "learn_result"
    ALLOWED_KEYS = {"type", "learn_id", "code", "error"}

    def to_json(self) -> dict[str, Any]:
        obj: dict[str, Any] = {"type": self.TYPE, "learn_id": self.learn_id}
        if self.code is not None:
            obj["code"] = self.code
        if self.error is not None:
            obj["error"] = self.error
        return obj

    @classmethod
    def from_json(cls, obj: Any) -> "LearnResultMsg":
        o = _require_mapping(obj, cls.TYPE)
        _reject_unknown(o, cls.ALLOWED_KEYS, cls.TYPE)
        _require_keys(o, {"learn_id"}, cls.TYPE)
        msg = cls(learn_id=_get_str(o, "learn_id", cls.TYPE))
        for key in ("code", "error"):
            if key in o and o[key] is not None:
                if not isinstance(o[key], str):
                    raise ProtocolError(f"{cls.TYPE}.{key} 应为字符串")
                object.__setattr__(msg, key, o[key])
        return msg


_FRAME_TYPES: dict[str, type] = {
    SnapshotMsg.TYPE: SnapshotMsg,
    HelloMsg.TYPE: HelloMsg,
    AuthOkMsg.TYPE: AuthOkMsg,
    AuthErrorMsg.TYPE: AuthErrorMsg,
    SnapshotAckMsg.TYPE: SnapshotAckMsg,
    PullMsg.TYPE: PullMsg,
    LearnStartMsg.TYPE: LearnStartMsg,
    LearnResultMsg.TYPE: LearnResultMsg,
}


def parse_frame(obj: Any) -> object:
    """按 `type` 判别并严格解析一帧；非法帧抛 ProtocolError（fail-closed）。"""
    o = _require_mapping(obj, "frame")
    frame_type = o.get("type")
    if not isinstance(frame_type, str) or frame_type not in _FRAME_TYPES:
        raise ProtocolError(f"未知消息类型: {frame_type!r}")
    return _FRAME_TYPES[frame_type].from_json(o)
