"""Fixtures for WB limiter/client tests: a fake clock and a spy sleep.

Both are pure in-memory, so token-bucket pacing is exercised in fake time and
tests never really wait.
"""

import pytest


class FakeClock:
    """Deterministic monotonic clock: starts at 0, moves only via advance()."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def advance(self, dt: float) -> None:
        """Move the clock forward by dt seconds."""
        self._now += dt

    def __call__(self) -> float:
        return self._now


class SpySleep:
    """asyncio.sleep stand-in: records durations and, when linked to a clock,
    advances it by the slept amount (so buckets refill instantly in tests)."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self.calls: list[float] = []
        self.last: float | None = None
        self.total = 0.0

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.last = seconds
        self.total += seconds
        if self.clock is not None:
            self.clock.advance(seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def spy_sleep(fake_clock: FakeClock) -> SpySleep:
    return SpySleep(fake_clock)
