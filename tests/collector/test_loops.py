"""IntervalLoop behavior tests: fake clock/sleep + real Postgres collection_log.

Clock/sleep fakes come from tests/wb/conftest.py (imported). db_session is the
real dev-Postgres session (loop_scope="session"), wrapped so IntervalLoop's
db_session_factory reuses it — repos.log_collection commits inside the
conftest savepoint, so every test stays isolated.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.loops import IntervalLoop
from app.crypto import encrypt_token
from app.models import Cabinet, CollectionLog
from app.wb.errors import WbApiError
from tests.wb.conftest import FakeClock, SpySleep

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _cabinet_id(db_session: AsyncSession) -> int:
    """A real cabinet row: collection_log.cabinet_id is a FK to cabinets."""
    cabinet = Cabinet(
        name="loops-test", wb_type="BY", currency="BYN",
        token_encrypted=encrypt_token("tok"),
    )
    db_session.add(cabinet)
    await db_session.commit()
    return cabinet.id


class _TimeUp(Exception):
    """Raised by the bounded clock to cut an infinite loop in fake time."""


class _BoundedClock(FakeClock):
    """FakeClock that raises _TimeUp once fake time reaches `limit` seconds."""

    def __init__(self, limit: float) -> None:
        super().__init__()
        self.limit = limit

    def advance(self, dt: float) -> None:
        super().advance(dt)
        if self() >= self.limit:
            raise _TimeUp


def _session_factory(session: AsyncSession):
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


async def _log_rows(session: AsyncSession, loop_name: str) -> list[CollectionLog]:
    result = await session.execute(
        select(CollectionLog)
        .where(CollectionLog.loop_name == loop_name)
        .order_by(CollectionLog.id)
    )
    return list(result.scalars().all())


async def test_run_once_calls_fn_exactly_once(db_session: AsyncSession) -> None:
    cab = await _cabinet_id(db_session)
    calls: list[int] = []

    async def fn() -> int:
        calls.append(1)
        return len(calls)

    spy = SpySleep()
    loop = IntervalLoop(
        "once", cab, 300, fn, clock=FakeClock(), sleep=spy,
        db_session_factory=_session_factory(db_session),
    )
    await loop.run_once()
    assert calls == [1]  # one call, no loop
    assert spy.calls == []  # run_once never sleeps
    rows = await _log_rows(db_session, "once")
    assert [row.status for row in rows] == ["ok"]


async def test_run_forever_first_immediately_then_every_period(
    db_session: AsyncSession,
) -> None:
    """period_sec=300 over 1200s of fake time: fn at t=0, 300, 600, 900."""
    cab = await _cabinet_id(db_session)
    clock = _BoundedClock(limit=1200)
    spy = SpySleep(clock)
    calls: list[float] = []

    async def fn() -> int:
        calls.append(clock())
        return len(calls)

    loop = IntervalLoop(
        "periodic", cab, 300, fn, clock=clock, sleep=spy,
        db_session_factory=_session_factory(db_session),
    )
    with pytest.raises(_TimeUp):  # the bound cuts run_forever in fake time
        await loop.run_forever()
    assert calls == [0.0, 300.0, 600.0, 900.0]  # first run immediately
    # one period sleep between runs; the 4th sleep starts and is cut by _TimeUp
    assert spy.calls == [300, 300, 300, 300]


async def test_success_logs_ok_with_processed_count(db_session: AsyncSession) -> None:
    cab = await _cabinet_id(db_session)

    async def fn() -> int:
        return 7

    loop = IntervalLoop(
        "good", cab, 60, fn, clock=FakeClock(), sleep=SpySleep(),
        db_session_factory=_session_factory(db_session),
    )
    await loop.run_once()
    rows = await _log_rows(db_session, "good")
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].detail is not None and "7" in rows[0].detail
    assert rows[0].cabinet_id == cab
    assert rows[0].started_at is not None and rows[0].finished_at is not None
    # timestamps are timezone-aware UTC
    assert rows[0].started_at.tzinfo is UTC and rows[0].finished_at.tzinfo is UTC


async def test_three_consecutive_errors_pause_loop(db_session: AsyncSession) -> None:
    """3rd consecutive failure: long sleep (>=900s, via spy) + "paused" row;
    the counter resets after the pause, so a 4th failure is a plain "error"."""
    cab = await _cabinet_id(db_session)
    spy = SpySleep()
    calls: list[int] = []

    async def fn() -> int:
        calls.append(1)
        raise RuntimeError("boom")

    loop = IntervalLoop(
        "flaky", cab, 300, fn, clock=FakeClock(), sleep=spy,
        db_session_factory=_session_factory(db_session),
    )
    for _ in range(3):
        await loop.run_once()
    assert calls == [1, 1, 1]
    assert spy.calls and spy.calls[-1] >= 900  # long pause sleep on the 3rd failure
    rows = await _log_rows(db_session, "flaky")
    assert [row.status for row in rows] == ["error", "error", "paused"]

    await loop.run_once()  # counter was reset after the pause
    rows = await _log_rows(db_session, "flaky")
    assert [row.status for row in rows] == ["error", "error", "paused", "error"]


async def test_run_forever_stops_on_403_without_retry(db_session: AsyncSession) -> None:
    """WbApiError(403): no retry, no sleep, loop stopped until a restart."""
    cab = await _cabinet_id(db_session)
    spy = SpySleep()
    calls: list[int] = []

    async def fn() -> int:
        calls.append(1)
        raise WbApiError(403, "scope is not allowed for this resource")

    loop = IntervalLoop(
        "forbidden", cab, 300, fn, clock=FakeClock(), sleep=spy,
        db_session_factory=_session_factory(db_session),
    )
    await loop.run_forever()  # returns instead of looping forever
    assert calls == [1]  # fn never called again
    assert spy.calls == []  # not even one period sleep
    assert loop.stopped is True
    rows = await _log_rows(db_session, "forbidden")
    assert [row.status for row in rows] == ["forbidden"]


async def test_run_once_on_stopped_loop_is_noop(db_session: AsyncSession) -> None:
    """After a 403 stop, run_once must not call fn again (until a restart)."""
    cab = await _cabinet_id(db_session)
    calls: list[int] = []

    async def fn() -> int:
        calls.append(1)
        raise WbApiError(403, "forbidden")

    loop = IntervalLoop(
        "halted", cab, 300, fn, clock=FakeClock(), sleep=SpySleep(),
        db_session_factory=_session_factory(db_session),
    )
    await loop.run_once()
    assert calls == [1] and loop.stopped is True
    await loop.run_once()
    assert calls == [1]  # no new call


async def test_log_write_failure_does_not_kill_loop(db_session: AsyncSession) -> None:
    """A broken db_session_factory must not crash run_once (loop survives)."""
    calls: list[int] = []

    async def fn() -> int:
        calls.append(1)
        return 1

    def broken_factory():
        raise RuntimeError("db down")

    loop = IntervalLoop(
        "nolog", 1, 60, fn, clock=FakeClock(), sleep=SpySleep(),
        db_session_factory=broken_factory,
    )
    await loop.run_once()
    assert calls == [1]
