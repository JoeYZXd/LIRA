"""家电控制层（U6）：设备/场景模型、SQLite 本地库、ir-ctl 学习/回放封装。

模块边界（计划 Output Structure）::

    appliances/models.py  设备/场景/码值模型（与 dialog.intents.Appliance 对齐）
    appliances/store.py   SQLite(WAL, synchronous=FULL) 本地持久化（R13）
    appliances/ir.py      发送安全闸（双层 fail-closed）、学习会话、场景逐项回放
"""

from __future__ import annotations

from lira.appliances.ir import (
    ApplianceError,
    IRService,
    LearningManager,
    SceneStepResult,
)
from lira.appliances.models import ApplianceModel, SceneModel, SceneStep
from lira.appliances.store import ApplianceStore

__all__ = [
    "ApplianceError",
    "ApplianceModel",
    "ApplianceStore",
    "IRService",
    "LearningManager",
    "SceneModel",
    "SceneStep",
    "SceneStepResult",
]
