"""U5 熔断降级器测试（Error path: 连续失败开断路 → 冷却后半开 → 成功闭合）。

时钟注入 FakeClock 虚拟推进冷却期，不真等 60s。
"""

from __future__ import annotations

import pytest

from lira.llm.breaker import BreakerState, CircuitBreaker


class FakeClock:
    """虚拟单调时钟：advance 手动推进，测试冷却期迁移。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(threshold: int = 3, cooldown: float = 60.0) -> tuple[CircuitBreaker, FakeClock]:
    clock = FakeClock()
    return (
        CircuitBreaker(failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock),
        clock,
    )


class TestClosedState:
    def test_initially_closed_and_available(self):
        breaker, _ = make_breaker()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.is_available() is True

    def test_failures_below_threshold_stay_closed(self):
        breaker, _ = make_breaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.is_available() is True

    def test_success_resets_failure_count(self):
        breaker, _ = make_breaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()  # 计数清零
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_available() is True  # 清零后 2 次 < 阈值 3


class TestOpenState:
    def test_threshold_consecutive_failures_open_circuit(self):
        breaker, _ = make_breaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert breaker.is_available() is False

    def test_open_blocks_until_cooldown_elapsed(self):
        breaker, clock = make_breaker(threshold=1, cooldown=60.0)
        breaker.record_failure()
        assert breaker.is_available() is False
        clock.advance(59.9)
        assert breaker.is_available() is False
        clock.advance(0.2)  # 累计 60.1s >= 冷却 60s
        assert breaker.is_available() is True

    def test_failure_while_open_refreshes_cooldown(self):
        breaker, clock = make_breaker(threshold=1, cooldown=60.0)
        breaker.record_failure()
        clock.advance(59.0)
        breaker.record_failure()  # OPEN 期间的失败重置冷却起点
        clock.advance(59.5)       # 距新起点不足 60s
        assert breaker.is_available() is False


class TestHalfOpenRecovery:
    def test_cooldown_elapsed_allows_half_open_probe(self):
        breaker, clock = make_breaker(threshold=3, cooldown=60.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(60.0)
        assert breaker.state is BreakerState.HALF_OPEN
        assert breaker.is_available() is True

    def test_half_open_success_closes_and_resets(self):
        breaker, clock = make_breaker(threshold=3, cooldown=60.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(60.0)
        breaker.record_success()
        assert breaker.state is BreakerState.CLOSED
        # 失败计数已清零：闭合后再失败 2 次不应开路
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_available() is True

    def test_half_open_failure_reopens_with_fresh_cooldown(self):
        breaker, clock = make_breaker(threshold=3, cooldown=60.0)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(60.0)
        assert breaker.is_available() is True  # HALF_OPEN
        breaker.record_failure()               # 试探失败
        assert breaker.state is BreakerState.OPEN
        assert breaker.is_available() is False
        clock.advance(59.0)
        assert breaker.is_available() is False  # 新冷却期未满
        clock.advance(1.5)
        assert breaker.is_available() is True   # 再次放行试探


class TestParamValidation:
    def test_zero_threshold_rejected(self):
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreaker(failure_threshold=0)

    def test_non_positive_cooldown_rejected(self):
        with pytest.raises(ValueError, match="cooldown_seconds"):
            CircuitBreaker(cooldown_seconds=0.0)
