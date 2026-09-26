"""熔断降级器（U5，R10）。

策略（计划 Key Decisions / Deferred 项）：
  - 连续失败 N 次（默认 3）→ OPEN，冷却 60s；期间 `is_available()=False`；
  - 冷却结束后进入 HALF_OPEN，放行一次试探请求；
    试探成功 → CLOSED（失败计数清零）；试探失败 → 重新 OPEN（重新计冷却）。
  - 时钟注入（`clock`）以便单元测试用虚拟时间推进冷却，不真等 60s。

与联网检测的关系：熔断状态只反映"最近请求成败"，联网探测（如 ping 网关）
由装配层注入 `LlmClient(network_ok=...)` 后与熔断状态合并为"远程可用性"
单一信号喂给路由层（R10），本模块不感知网络。
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable

__all__ = ["BreakerState", "CircuitBreaker"]


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """连续失败计数熔断器。线程安全不设防：设备端为单 asyncio 事件循环。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds 必须为正数")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._opened_at: float | None = None

    @property
    def state(self) -> BreakerState:
        # OPEN 是否已到冷却期，惰性迁移到 HALF_OPEN（放行一次试探）
        if self._state is BreakerState.OPEN and self._cooldown_elapsed:
            self._state = BreakerState.HALF_OPEN
        return self._state

    @property
    def _cooldown_elapsed(self) -> bool:
        assert self._opened_at is not None
        return (self._clock() - self._opened_at) >= self._cooldown_seconds

    def is_available(self) -> bool:
        """远程可用的熔断侧信号：CLOSED / HALF_OPEN（试探）为 True。"""
        return self.state is not BreakerState.OPEN

    def record_success(self) -> None:
        """请求成功：CLOSED 化，失败计数清零（HALF_OPEN 试探成功即闭合）。"""
        self._failures = 0
        self._opened_at = None
        self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        """请求失败：CLOSED 累计到阈值开断路；HALF_OPEN 试探失败重开冷却。"""
        if self._state is BreakerState.HALF_OPEN:
            self._open()
            return
        self._failures += 1
        if self._state is BreakerState.OPEN or self._failures >= self._failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._failures = self._failure_threshold  # 保持"已连续失败满额"语义
