"""设备/场景/码值模型（U6）：纯数据结构 + 与协议 DTO / 意图层模型的双向对齐。

字段纪律（计划 U6 Approach）：
  - 名称、别名（语音匹配词）、动作触发词、码值（原始 pulse/space）、
    is_high_risk、enabled；
  - 码值是设备本地学习产物（R32），不经快照下发，故与 protocol.ApplianceDTO
    分离：DTO 描述"后台配置面"，模型额外携带 codes；
  - 与 `lira.dialog.intents.Appliance` 字段名保持对齐（U3 时的约定），
    `to_dialog()` 供 Router/状态机本地规则匹配使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lira import protocol
from lira.dialog.intents import Appliance as DialogAppliance

__all__ = ["SceneStep", "ApplianceModel", "SceneModel"]


@dataclass
class SceneStep:
    """场景中的一步：某设备的某动作。"""

    device: str
    action: str


@dataclass
class ApplianceModel:
    """家电条目（含本地学习到的码值）。

    Attributes:
        name: 设备唯一名（后台配置主键）。
        aliases: 语音匹配别名（别名唯一命中才执行，G12）。
        actions: 动作名 -> 语音触发词元组（取最长触发词）。
        codes: 动作名 -> 原始码（ir-ctl pulse/space 文本）；重学习 UPSERT 覆盖。
        is_high_risk: 高危标记（R6/R23 二次确认；确认逻辑是代码常量）。
        enabled: 家人配置的启用开关（R7，禁用即拒发）。
    """

    name: str
    aliases: tuple[str, ...] = ()
    actions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    is_high_risk: bool = False
    enabled: bool = True

    def to_dialog(self) -> DialogAppliance:
        """转 U3 意图层模型（Router 本地规则匹配输入）。码值不进入意图层。"""
        return DialogAppliance(
            name=self.name,
            aliases=self.aliases,
            actions=dict(self.actions),
            is_high_risk=self.is_high_risk,
            enabled=self.enabled,
        )

    def to_dto(self) -> protocol.ApplianceDTO:
        """转协议 DTO（仅配置面，不含码值）。"""
        return protocol.ApplianceDTO(
            name=self.name,
            aliases=self.aliases,
            actions=dict(self.actions),
            is_high_risk=self.is_high_risk,
            enabled=self.enabled,
        )

    @classmethod
    def from_dto(cls, dto: protocol.ApplianceDTO, *, codes: dict[str, str] | None = None) -> "ApplianceModel":
        return cls(
            name=dto.name,
            aliases=dto.aliases,
            actions=dict(dto.actions),
            codes=dict(codes or {}),
            is_high_risk=dto.is_high_risk,
            enabled=dto.enabled,
        )


@dataclass
class SceneModel:
    """场景 = 设备动作列表（Key Decisions：空调按整状态帧，动作名即目标状态）。"""

    name: str
    steps: tuple[SceneStep, ...] = ()

    def to_dto(self) -> protocol.SceneDTO:
        return protocol.SceneDTO(
            name=self.name,
            steps=tuple(protocol.SceneStepDTO(device=s.device, action=s.action) for s in self.steps),
        )

    @classmethod
    def from_dto(cls, dto: protocol.SceneDTO) -> "SceneModel":
        return cls(name=dto.name, steps=tuple(SceneStep(device=s.device, action=s.action) for s in dto.steps))
