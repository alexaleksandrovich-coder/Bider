"""IntervalLoop: a named background collection loop with error bookkeeping.

Behavior (see tests/collector/test_loops.py):
- run_forever() runs fn immediately, then sleeps period_sec between runs;
- run_once() performs exactly one fn attempt — no looping;
- each attempt writes a collection_log row ("ok" / "error" / "paused" /
  "forbidden") through repos.log_collection using the injected
  db_session_factory;
- max_consecutive_errors failures in a row trigger a long pause
  (long_sleep_sec, 15 min by default) and a "paused" row; the counter resets
  after the pause;
- WbApiError with status 403 (token revoked / scope missing) is never
  retried: the loop marks itself stopped for good and stops calling fn until
  a new IntervalLoop instance is created (a restart);
- non-WbApiError failures are logged via logging.getLogger(__name__).exception.

Clock and sleep are injectable (fake time in tests); a stopped loop is
terminated by run_forever and skipped by run_once.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.db import SessionLocal
from app.repos import log_collection
from app.wb.errors import WbApiError

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
SessionFactory = Callable[[], object]  # async context manager -> AsyncSession


class IntervalLoop:
    """One periodic collector: fn + period + error/pause/forbidden handling."""

    def __init__(
        self,
        name: str,
        cabinet_id: int | None,
        period_sec: int,
        fn: Callable[[], Awaitable[int | None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        db_session_factory: SessionFactory = SessionLocal,
        long_sleep_sec: float = 900,
        max_consecutive_errors: int = 3,
    ) -> None:
        self.name = name
        self.cabinet_id = cabinet_id
        self.period_sec = period_sec
        self._fn = fn
        self._clock = clock  # kept for symmetry/instrumentation; timing goes through sleep
        self._sleep = sleep
        self._db_session_factory = db_session_factory
        self._long_sleep_sec = long_sleep_sec
        self._max_consecutive_errors = max_consecutive_errors
        self._consecutive_errors = 0
        self.stopped = False

    async def run_once(self) -> None:
        """Run fn once and record the outcome in collection_log. No looping."""
        if self.stopped:  # forbidden-stopped loop: no fn calls until a restart
            return
        started_at = datetime.now(UTC)
        try:
            processed = await self._fn()
        except WbApiError as exc:
            if exc.status == 403:
                self.stopped = True
                logger.error(
                    "loop %s (cabinet %s) stopped: WB 403, not retrying: %s",
                    self.name, self.cabinet_id, exc,
                )
                await self._write_log("forbidden", str(exc), started_at)
                return
            logger.warning("loop %s: WB API error: %s", self.name, exc)
            await self._register_failure(str(exc), started_at)
        except Exception as exc:
            logger.exception("loop %s failed", self.name)
            await self._register_failure(str(exc), started_at)
        else:
            self._consecutive_errors = 0
            detail = f"processed={processed}" if processed is not None else None
            await self._write_log("ok", detail, started_at)

    async def run_forever(self) -> None:
        """Run fn immediately, then every period_sec until stopped."""
        while not self.stopped:
            await self.run_once()
            if self.stopped:
                break
            await self._sleep(self.period_sec)

    # -- internals -----------------------------------------------------------

    async def _register_failure(self, detail: str, started_at: datetime) -> None:
        """Count a failure; at max consecutive failures log "paused" and sleep long."""
        self._consecutive_errors += 1
        if self._consecutive_errors < self._max_consecutive_errors:
            await self._write_log("error", detail, started_at)
            return
        self._consecutive_errors = 0  # pause resets the streak
        logger.error(
            "loop %s (cabinet %s): %d consecutive failures, pausing %.0fs",
            self.name, self.cabinet_id, self._max_consecutive_errors, self._long_sleep_sec,
        )
        await self._write_log("paused", detail, started_at)
        await self._sleep(self._long_sleep_sec)

    async def _write_log(
        self, status: str, detail: str | None, started_at: datetime
    ) -> None:
        """Append one collection_log row; a failing DB never kills the loop."""
        try:
            async with self._db_session_factory() as session:
                await log_collection(
                    session,
                    self.cabinet_id,
                    self.name,
                    status,
                    detail,
                    started_at,
                    datetime.now(UTC),
                )
        except Exception:
            logger.exception("loop %s: failed to write collection_log", self.name)
