"""Supervisor tests: cabinet set reconciliation against real dev-Postgres.

The supervisor (runner.supervisor_tick / start_for_cabinet / stop_for_cabinet)
reads cabinets through app.db.SessionLocal, so seeded cabinets must be
COMMITTED (unlike the savepoint-isolated db_session fixture); _with_db runs
session work on the shared session-scoped loop with a throwaway engine.

Loops started by the supervisor call the WB client immediately; WbClient is
monkeypatched with an offline fake so no network is touched. Every test stops
the loops it started and deletes its cabinets (FK CASCADE cleans children).
"""

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.collector import runner
from app.crypto import encrypt_token
from app.models import Cabinet
from tests.helpers import DEFAULT_TOKEN

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module", loop_scope="session", autouse=True)
async def app_engine_hygiene():
    """Dispose the app engine pool before/after this module.

    The supervisor's loops write through the app-wide SessionLocal singleton;
    its pooled connections bind to the (session) event loop they were created
    on. Cancelling a loop task mid-write can leave its connection to be
    returned to the pool lazily by GC — after a plain dispose — which would
    poison the TestClient-based web tests sharing this engine afterwards.
    So: GC first, then dispose, on both edges of the module.
    """
    import gc

    from app.db import dispose_engine

    gc.collect()
    await dispose_engine()
    yield
    gc.collect()
    await asyncio.sleep(0)  # let GC-returned connections settle into the pool
    await dispose_engine()


class _OfflineWb:
    """WbClient stand-in: every read returns an empty payload, no network."""

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    async def promotion_count(self) -> dict:
        return {"adverts": []}

    async def adverts(self, ids: list[int]) -> list[dict]:
        return []

    async def budgets(self, advert_ids: list[int]) -> list[dict]:
        return []

    async def balance(self) -> dict:
        return {"total": 0}

    async def fullstats(self, advert_ids: list[int], begin, end) -> list[dict]:
        return []

    async def bids_min(self, nm_ids: list[int]) -> dict:
        return {}

    async def recommendations(self, params: dict) -> dict:
        return {}

    async def normquery_list(self, advert_id: int) -> dict:
        return {}

    async def normquery_stats(self, body: dict) -> dict:
        return {}

    async def aclose(self) -> None:
        self.closed = True


async def _with_db(coro_fn):
    """Run `async def coro_fn(db)` on the current loop with a throwaway engine.

    (The web tests use asyncio.run because their tests are sync; here the
    tests are async, so the session work must await on the running loop.)
    """
    engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            return await coro_fn(db)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def schema():
    """Create tables if missing (create_all is idempotent)."""

    async def _create():
        import app.models  # noqa: F401  -- registers mappers on Base.metadata
        from app.db import Base

        engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(_create())


@pytest_asyncio.fixture(loop_scope="session")
async def clean_supervisor(monkeypatch: pytest.MonkeyPatch):
    """Offline WB client + empty supervisor registry before and after.

    Async ON PURPOSE: the loop tasks started by the supervisor live in the
    test's event loop, so teardown must stop them on that same loop (a sync
    teardown's fresh asyncio.run loop cannot cancel them). Yields a list the
    test appends its cabinet ids to; teardown stops the loops and deletes the
    cabinets (FK CASCADE cleans collection_log / raw_responses children).
    """
    monkeypatch.setattr(runner, "WbClient", _OfflineWb)
    ids: list[int] = []

    for cabinet_id in list(runner._running):
        await runner.stop_for_cabinet(cabinet_id)
    yield ids

    for cabinet_id in list(runner._running):
        await runner.stop_for_cabinet(cabinet_id)
    if ids:
        from sqlalchemy import delete

        engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                await db.execute(delete(Cabinet).where(Cabinet.id.in_(ids)))
                await db.commit()
        finally:
            await engine.dispose()


async def _seed_cabinet(name: str, token: str = DEFAULT_TOKEN, active: bool = True) -> int:
    async def _work(db):
        cabinet = Cabinet(
            name=name,
            wb_type="BY",
            currency="BYN",
            token_encrypted=encrypt_token(token),
            active=active,
        )
        db.add(cabinet)
        await db.commit()
        return cabinet.id

    return await _with_db(_work)


async def _set_token(cabinet_id: int, token: str) -> None:
    async def _work(db):
        cabinet = await db.get(Cabinet, cabinet_id)
        cabinet.token_encrypted = encrypt_token(token)
        await db.commit()

    await _with_db(_work)


async def _set_active(cabinet_id: int, active: bool) -> None:
    async def _work(db):
        cabinet = await db.get(Cabinet, cabinet_id)
        cabinet.active = active
        await db.commit()

    await _with_db(_work)


async def _delete_cabinet(cabinet_id: int) -> None:
    async def _work(db):
        cabinet = await db.get(Cabinet, cabinet_id)
        await db.delete(cabinet)
        await db.commit()

    await _with_db(_work)


async def _get_cabinet(cabinet_id: int) -> Cabinet:
    async def _work(db):
        return await db.get(Cabinet, cabinet_id)

    return await _with_db(_work)


# --- start_for_cabinet / stop_for_cabinet ---------------------------------------


async def test_start_for_cabinet_starts_seven_loops(schema, clean_supervisor):
    cid = await _seed_cabinet("sup-start")
    clean_supervisor.append(cid)

    loops = await runner.start_for_cabinet(await _get_cabinet(cid))

    assert len(loops) == 7
    assert {loop.name for loop in loops} == {
        "campaigns", "budgets", "balance", "fullstats",
        "bids_min", "auction_snapshot", "clusters",
    }
    entries = runner._running[cid]
    assert len(entries) == 7
    # every entry carries the same token fingerprint (sha256 of the ciphertext)
    fingerprint = runner._token_fingerprint((await _get_cabinet(cid)).token_encrypted)
    assert {fp for _, _, fp in entries} == {fingerprint}
    assert all(not task.done() for task, _, _ in entries)

    await runner.stop_for_cabinet(cid)
    assert cid not in runner._running
    assert all(task.cancelled() or task.done() for task, _, _ in entries)


async def test_stop_for_cabinet_unknown_id_is_noop(schema, clean_supervisor):
    await runner.stop_for_cabinet(987654321)  # must not raise
    assert 987654321 not in runner._running


# --- supervisor_tick -------------------------------------------------------------


async def test_tick_starts_loops_for_new_cabinet(schema, clean_supervisor):
    cid = await _seed_cabinet("sup-new")
    clean_supervisor.append(cid)

    changes = await runner.supervisor_tick()

    assert changes >= 1
    assert cid in runner._running
    assert len(runner._running[cid]) == 7

    # no changes since: a second tick is a no-op
    entries = list(runner._running[cid])
    assert await runner.supervisor_tick() == 0
    assert runner._running[cid] == entries


async def test_tick_restarts_cabinet_after_token_change(schema, clean_supervisor):
    cid = await _seed_cabinet("sup-retoken", token="first-token")
    clean_supervisor.append(cid)
    assert await runner.supervisor_tick() >= 1
    old_tasks = {task for task, _, _ in runner._running[cid]}
    old_fingerprint = {fp for _, _, fp in runner._running[cid]}

    await _set_token(cid, "second-token")
    changes = await runner.supervisor_tick()

    assert changes == 1
    entries = runner._running[cid]
    assert len(entries) == 7
    new_tasks = {task for task, _, _ in entries}
    assert new_tasks.isdisjoint(old_tasks)  # loops recreated, not reused
    assert all(not task.done() for task in new_tasks)
    new_fingerprint = {fp for _, _, fp in entries}
    assert new_fingerprint != old_fingerprint
    assert new_fingerprint == {
        runner._token_fingerprint((await _get_cabinet(cid)).token_encrypted)
    }
    assert all(task.cancelled() for task in old_tasks)


async def test_tick_stops_loops_of_deleted_cabinet(schema, clean_supervisor):
    cid = await _seed_cabinet("sup-deleted")
    clean_supervisor.append(cid)
    assert await runner.supervisor_tick() >= 1
    assert cid in runner._running

    await _delete_cabinet(cid)
    changes = await runner.supervisor_tick()

    assert changes == 1
    assert cid not in runner._running


async def test_tick_stops_loops_of_deactivated_cabinet(schema, clean_supervisor):
    cid = await _seed_cabinet("sup-inactive")
    clean_supervisor.append(cid)
    assert await runner.supervisor_tick() >= 1
    assert cid in runner._running

    await _set_active(cid, False)
    changes = await runner.supervisor_tick()

    assert changes == 1
    assert cid not in runner._running


# --- supervisor_forever ------------------------------------------------------------


async def test_supervisor_forever_ticks_immediately_then_sleeps(
    schema, clean_supervisor, monkeypatch: pytest.MonkeyPatch
):
    ticks = {"n": 0}

    async def fake_tick() -> int:
        ticks["n"] += 1
        return 0

    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError  # stop the supervisor after 2 periods

    monkeypatch.setattr(runner, "supervisor_tick", fake_tick)
    with pytest.raises(asyncio.CancelledError):
        await runner.supervisor_forever(sleep=fake_sleep, period_sec=60)

    assert ticks["n"] == 2  # first tick immediately, then tick-sleep-tick
    assert sleeps == [60, 60]  # every period slept exactly once, in order


# --- sync_cabinet_once --------------------------------------------------------------


async def test_sync_cabinet_once_runs_all_steps_and_logs(schema, clean_supervisor):
    from sqlalchemy import select

    from app.models import CollectionLog

    cid = await _seed_cabinet("sup-sync")
    clean_supervisor.append(cid)

    result = await runner.sync_cabinet_once(cid)

    assert set(result) == {"campaigns", "budgets", "balance", "fullstats"}
    # offline fake: campaigns/budgets/fullstats have nothing to fetch (0 rows);
    # balance always writes one observation from {"total": 0}
    assert result["campaigns"] == 0
    assert result["budgets"] == 0
    assert result["fullstats"] == 0
    assert isinstance(result["balance"], int) and result["balance"] >= 1

    async def _logs(db):
        return (
            (
                await db.execute(
                    select(CollectionLog).where(CollectionLog.cabinet_id == cid)
                )
            )
            .scalars()
            .all()
        )

    logs = await _with_db(_logs)
    by_name = {log.loop_name: log for log in logs}
    assert set(by_name) == {"campaigns", "budgets", "balance", "fullstats"}
    assert all(log.status == "ok" for log in by_name.values())
    assert all(log.finished_at is not None for log in by_name.values())


async def test_sync_cabinet_once_missing_cabinet_raises(schema):
    with pytest.raises(ValueError):
        await runner.sync_cabinet_once(987654321)
