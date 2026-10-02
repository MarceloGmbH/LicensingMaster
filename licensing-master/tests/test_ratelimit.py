"""Pure unit tests for the in-memory failure limiter (no DB, no network)."""

from __future__ import annotations

import pytest

from src.ratelimit import FailureLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(clock: FakeClock, max_failures: int = 3, window: int = 60, lockout: int = 120) -> FailureLimiter:
    return FailureLimiter(max_failures, window, lockout, clock=clock)


def test_allows_until_threshold_then_locks_out() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(2):
        assert lim.retry_after("1.2.3.4") == 0
        lim.record_failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 0
    lim.record_failure("1.2.3.4")  # third failure trips the lockout
    assert lim.retry_after("1.2.3.4") == 120


def test_lockout_expires_and_counter_restarts() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(3):
        lim.record_failure("ip")
    clock.advance(119)
    assert lim.retry_after("ip") == 1
    clock.advance(2)
    assert lim.retry_after("ip") == 0
    lim.record_failure("ip")  # a fresh count, not an instant relock
    assert lim.retry_after("ip") == 0


def test_failures_outside_the_window_do_not_accumulate() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    lim.record_failure("ip")
    lim.record_failure("ip")
    clock.advance(61)
    lim.record_failure("ip")  # the first two aged out
    assert lim.retry_after("ip") == 0


def test_keys_are_isolated() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(3):
        lim.record_failure("attacker")
    assert lim.retry_after("attacker") > 0
    assert lim.retry_after("honest") == 0


def test_reset_clears_state() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(3):
        lim.record_failure("ip")
    lim.reset()
    assert lim.retry_after("ip") == 0


def test_memory_is_bounded() -> None:
    clock = FakeClock()
    lim = FailureLimiter(3, 60, 120, clock=clock, max_keys=50)
    for i in range(500):
        lim.record_failure(f"ip-{i}")
        clock.advance(0.01)
    assert lim.tracked_keys() <= 50


def test_rejects_nonsensical_config() -> None:
    with pytest.raises(ValueError):
        FailureLimiter(0, 60, 60)
