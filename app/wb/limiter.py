"""Async rate limiting: classic token bucket + per-(cabinet, class) registry.

Clock and sleep are injectable so tests run in fake time (see
tests/wb/conftest.py); production uses time.monotonic and asyncio.sleep.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

from app.wb.constants import LIMITS

Sleep = Callable[[float], Awaitable[None]]


class AsyncTokenBucket:
    """Token bucket: burst = full bucket, constant refill of requests/period_sec.

    Starts with `requests` tokens (full burst allowed immediately); every
    elapsed second adds requests/period_sec tokens up to capacity. acquire()
    consumes one token or, via the injected sleep, waits until one is available.
    """

    def __init__(
        self,
        requests: int,
        period_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if requests <= 0:
            raise ValueError("requests must be positive")
        if period_sec <= 0:
            raise ValueError("period_sec must be positive")
        self.requests = requests
        self.period_sec = period_sec
        self._rate = requests / period_sec  # tokens per second
        self._capacity = float(requests)
        self._tokens = float(requests)
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()

    async def acquire(self) -> None:
        """Consume one token, sleeping through a refill if the bucket is empty."""
        while True:
            now = self._clock()
            elapsed = now - self._updated_at
            if elapsed > 0:
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated_at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            deficit = (1.0 - self._tokens) / self._rate
            await self._sleep(deficit)


class LimiterRegistry:
    """Lazily creates one AsyncTokenBucket per (cabinet_id, limit class)."""

    def __init__(
        self,
        limits: Mapping[str, tuple[int, float]] = LIMITS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[tuple[int, str], AsyncTokenBucket] = {}

    def bucket(self, cabinet_id: int, klass: str) -> AsyncTokenBucket:
        """Return (creating on first use) the bucket for this cabinet+class."""
        key = (cabinet_id, klass)
        bucket = self._buckets.get(key)
        if bucket is None:
            try:
                requests, period_sec = self._limits[klass]
            except KeyError:
                raise ValueError(f"unknown limit class: {klass!r}") from None
            bucket = AsyncTokenBucket(requests, period_sec, clock=self._clock, sleep=self._sleep)
            self._buckets[key] = bucket
        return bucket
