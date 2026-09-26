"""ir-ctl 学习/回放封装（U6）：经 HAL IrController 收发，安全闸在 appliances 层。

安全设计（计划 U6 Approach「双层 fail-closed」）：
  - 第一层：状态机（U3）——高危设备进 CONFIRMING，确认词命中才回调 send_ir；
  - 第二层（本模块 `IRService.send_ir`）：发送前再查 enabled / 高危确认标记，
    绕过状态机的防御性直调同样被拒——`is_high_risk and not confirmed` 一律拒发。

红外原始码方案（Key Decisions）：学习 = 录原始 pulse/space 存库；
回放 = 发原始码；空调按"整状态帧"学习（每种目标状态一帧，不做状态组合）。
板上 ir-ctl 子进程实现留 U10，本单元经 HAL 接口 + mock。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from lira.appliances.models import ApplianceModel
from lira.appliances.store import ApplianceStore
from lira.hal.base import IrController

__all__ = ["ApplianceError", "SceneStepResult", "IRService", "LearningManager"]


class ApplianceError(Exception):
    """家电层错误。kind 供上层（状态机/同步）转语音话术或回执，不抛裸异常。

    kind 取值: device_not_found / action_not_found / disabled /
    confirm_required / code_missing / scene_not_found / learning_busy / learn_failed
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class SceneStepResult:
    """场景单步执行结果（R29：逐项报告，措辞纪律由上层 phrasebook 承担）。"""

    device: str
    action: str
    ok: bool
    error_kind: str | None = None


class IRService:
    """红外学习/回放服务：store（本地库）+ IrController（HAL）的组合封装。"""

    def __init__(self, store: ApplianceStore, ir: IrController, *, learn_timeout: float = 10.0) -> None:
        self._store = store
        self._ir = ir
        self._learn_timeout = learn_timeout

    # ---------- 回放（含最后一道安全闸） ----------

    async def send_ir(self, device: str, action: str, *, confirmed: bool = False) -> None:
        """发送一帧原始码。前置校验顺序：设备存在 → enabled → 动作/码值 → 高危确认。

        任何校验失败抛 ApplianceError，IR 绝不发射（fail-closed）。
        """
        model = self._store.get_appliance(device)
        if model is None:
            raise ApplianceError("device_not_found", f"设备未配置: {device}")
        if not model.enabled:
            # R7/AE3：禁用设备拒发（防御性直调也在此被拦）
            raise ApplianceError("disabled", f"设备已被家人禁用: {device}")
        if action not in model.actions:
            raise ApplianceError("action_not_found", f"设备 {device} 无动作: {action}")
        code = model.codes.get(action)
        if not code:
            raise ApplianceError("code_missing", f"设备 {device} 动作 {action} 尚未学习红外码")
        if model.is_high_risk and not confirmed:
            # R6/R23：高危未确认一律拒发——状态机之外的最后一道闸
            raise ApplianceError("confirm_required", f"高危设备 {device} 未确认，拒发")
        await self._ir.send(code)
        logging.debug("IR 已发送: device=%s action=%s", device, action)

    async def run_scene(self, scene_name: str, *, confirmed: bool = False) -> list[SceneStepResult]:
        """逐项回放场景步骤并逐项报告（R29）。单步失败不中断后续步骤。"""
        scene = self._store.get_scene(scene_name)
        if scene is None:
            raise ApplianceError("scene_not_found", f"场景未配置: {scene_name}")
        results: list[SceneStepResult] = []
        for step in scene.steps:
            try:
                await self.send_ir(step.device, step.action, confirmed=confirmed)
                results.append(SceneStepResult(step.device, step.action, ok=True))
            except ApplianceError as exc:
                logging.warning("场景步骤失败: scene=%s step=%s/%s kind=%s",
                                scene_name, step.device, step.action, exc.kind)
                results.append(SceneStepResult(step.device, step.action, ok=False, error_kind=exc.kind))
        return results

    # ---------- 学习（R32：后台发起 → 设备录一帧 → 入库） ----------

    async def learn(self, device: str, action: str) -> str:
        """录一帧原始码并 UPSERT 入库（重学习覆盖同设备+动作）。"""
        if self._store.get_appliance(device) is None:
            raise ApplianceError("device_not_found", f"设备未配置: {device}")
        try:
            code = await self._ir.learn(self._learn_timeout)
        except Exception as exc:  # HAL 统一转 ApplianceError，不向上抛裸异常
            raise ApplianceError("learn_failed", f"红外学习失败: {exc}") from exc
        if not code:
            raise ApplianceError("learn_failed", "红外学习返回空码值")
        self._store.upsert_code(device, action, code)
        logging.debug("IR 学习入库: device=%s action=%s", device, action)
        return code


class LearningManager:
    """学习会话管理（R32）：同一时刻至多一个学习会话；后台经 sync 消息触发。

    用法::

        task = manager.begin("空调", "制冷26度")   # 进入学习模式
        code = await task                           # 等待码值（mock 队列/真机遥控器）
    """

    def __init__(self, service: IRService) -> None:
        self._service = service
        self._task: asyncio.Task[str] | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def begin(self, device: str, action: str) -> asyncio.Task[str]:
        """进入学习模式：后台录码任务；已有进行中的会话则拒绝（learning_busy）。"""
        if self.active:
            raise ApplianceError("learning_busy", "已有进行中的学习会话")
        self._task = asyncio.create_task(self._service.learn(device, action))
        return self._task
