"""三级意图路由（U3）：本地规则 → 本地 LLM → 远程 LLM（计划 Key Technical Decisions）。

- Tier 1 本地规则：`intents.match_local`，永远最先且必经；高危确认词表在本地层，
  永不经 LLM（R6/R23）。
- Tier 2 本地 LLM：Phase 1 为注入接口（可为 None = 未配置，直接降级跳过）。
- Tier 3 远程 LLM：注入 async handler + 可用性信号（U5 接入 OpenAI 兼容客户端
  与熔断器后，`remote_available` 即"联网 + 熔断闭合"的单一信号，R10）。
- 降级 = UNAVAILABLE 结果（上层按 R27 只读原文 / R24 引导处理），路由层不抛异常。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Iterable

from lira.dialog.intents import Appliance, Intent, match_local

__all__ = ["RouterTier", "RouteResult", "Router", "LlmHandler"]

#: LLM handler 约定：返回回复文本；返回 None = 无法处理（继续降级）
LlmHandler = Callable[[str], Awaitable[str | None]]


class RouterTier(Enum):
    LOCAL_RULE = "local_rule"
    LOCAL_LLM = "local_llm"
    REMOTE_LLM = "remote_llm"
    UNAVAILABLE = "unavailable"


@dataclass
class RouteResult:
    tier: RouterTier
    intent: Intent | None = None
    reply: str | None = None


class Router:
    """三级路由器：纯调度逻辑，LLM 能力全部注入（可全面单测）。

    `appliances` 可传静态序列，也可传 ``Callable[[], Iterable[Appliance]]``
    提供者（U9 装配层注入"每次匹配现读本地库"的取数函数，使同步快照
    更新（U6/AE7）后语音匹配立即生效，无需重建 Router）。
    """

    def __init__(
        self,
        appliances: Iterable[Appliance] | Callable[[], Iterable[Appliance]] = (),
        local_llm: LlmHandler | None = None,
        remote_llm: LlmHandler | None = None,
        remote_available: Callable[[], bool] = lambda: False,
    ) -> None:
        self._appliances = appliances if callable(appliances) else tuple(appliances)
        self._local_llm = local_llm
        self._remote_llm = remote_llm
        self._remote_available = remote_available

    @property
    def appliances(self) -> tuple[Appliance, ...]:
        """当前家电表（U6 状态机澄清/未配置细分检查复用同一份）。"""
        return tuple(self._appliances()) if callable(self._appliances) else self._appliances  # type: ignore[return-value]

    async def route(self, text: str) -> RouteResult:
        # Tier 1: 本地规则（家电/阅读/帮助/取消）；家电表经 property 现读
        # （callable 提供者 = 每次匹配取本地库最新快照，U6/AE7 同步即时生效）
        intent = match_local(text, self.appliances)
        if intent is not None:
            return RouteResult(RouterTier.LOCAL_RULE, intent=intent)

        # Tier 2: 本地 LLM（未配置则跳过 = 可配置降级）
        if self._local_llm is not None:
            reply = await self._local_llm(text)
            if reply:
                return RouteResult(RouterTier.LOCAL_LLM, reply=reply)

        # Tier 3: 远程 LLM（可用性信号不满足时绝不发起调用）
        if self._remote_llm is not None and self._remote_available():
            reply = await self._remote_llm(text)
            if reply:
                return RouteResult(RouterTier.REMOTE_LLM, reply=reply)

        logging.debug("路由降级: 全部层级无结果")
        return RouteResult(RouterTier.UNAVAILABLE)
