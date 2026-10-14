"""Repository tests against the real test Postgres (db_session from conftest).

Each repos.* call commits; the conftest savepoint recipe keeps every test
isolated (the outer transaction is rolled back at teardown).
"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.crypto import encrypt_token
from app.models import (
    AuctionSnapshot,
    Balance,
    BidHistory,
    Budget,
    Cabinet,
    Campaign,
    Cluster,
    CollectionLog,
    Position,
    Stat,
)
from app.repos import (
    append_bids,
    insert_balance,
    insert_budgets,
    insert_snapshot,
    log_collection,
    upsert_campaigns,
    upsert_clusters,
    upsert_positions,
    upsert_stats,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
_MSK = timezone(timedelta(hours=3))

_CAMPAIGN_ROW = {
    "advert_id": 1001,
    "name": "Поиск-1",
    "type": 8,
    "status": 9,
    "payment_type": "cpm",
    "bid_type": "manual",
    "search": True,
    "recommendations": False,
    "change_time": datetime(2026, 10, 9, 12, 30, tzinfo=_MSK),
    "bids": [{"nm_id": 111, "placement": "search", "bid_kopecks": 25000}],
}

_STAT_ROW = {
    "advert_id": 1,
    "nm_id": 111,
    "date": date(2026, 10, 10),
    "app_type": "web",
    "views": 100,
    "clicks": 5,
    "atbs": 2,
    "orders": 1,
    "shks": 1,
    "sum_kopecks": 2500,
    "sum_price_kopecks": 50000,
    "cpc_kopecks": 500,
    "canceled": 0,
}


async def _cabinet_id(db_session) -> int:
    cabinet = Cabinet(name="BY-1", wb_type="BY", currency="BYN", token_encrypted=encrypt_token("tok"))
    db_session.add(cabinet)
    await db_session.commit()
    return cabinet.id


async def test_upsert_campaigns_twice_updates_in_place(db_session):
    cabinet_id = await _cabinet_id(db_session)

    assert await upsert_campaigns(db_session, cabinet_id, [dict(_CAMPAIGN_ROW)]) == 1

    changed = {**_CAMPAIGN_ROW, "name": "Переименована", "status": 11, "recommendations": True}
    assert await upsert_campaigns(db_session, cabinet_id, [changed]) == 1

    rows = (await db_session.execute(select(Campaign))).scalars().all()
    assert len(rows) == 1  # upsert, not a second row
    row = rows[0]
    assert (row.name, row.status) == ("Переименована", 11)
    assert row.placement_search is True
    assert row.placement_reco is True
    assert row.updated_at is not None
    assert row.change_time == datetime(2026, 10, 9, 12, 30, tzinfo=_MSK)


async def test_append_bids_twice_appends_two_rows(db_session):
    cabinet_id = await _cabinet_id(db_session)
    rows = [dict(_CAMPAIGN_ROW)]

    assert await append_bids(db_session, cabinet_id, rows, _T0) == 1
    assert await append_bids(db_session, cabinet_id, rows, _T0 + timedelta(minutes=5)) == 1

    bids = (
        await db_session.execute(select(BidHistory).order_by(BidHistory.captured_at))
    ).scalars().all()
    assert len(bids) == 2  # append-only history: two observations
    assert [(b.nm_id, b.placement, b.bid_kopecks) for b in bids] == [(111, "search", 25000)] * 2
    assert bids[0].captured_at < bids[1].captured_at


async def test_upsert_stats_is_idempotent(db_session):
    cabinet_id = await _cabinet_id(db_session)
    rows = [dict(_STAT_ROW)]

    assert await upsert_stats(db_session, cabinet_id, rows) == 1
    assert await upsert_stats(db_session, cabinet_id, rows) == 1  # same data again

    stats = (await db_session.execute(select(Stat))).scalars().all()
    assert len(stats) == 1  # row count did not grow
    row = stats[0]
    assert (row.views, row.clicks, row.sum_kopecks, row.cpc_kopecks) == (100, 5, 2500, 500)
    assert row.sum_price_kopecks == 50000


async def test_insert_budgets_then_read_latest_by_captured_at(db_session):
    cabinet_id = await _cabinet_id(db_session)
    row = {"advert_id": 1, "cash": 100000, "netting": 50000, "total": 150000}

    assert await insert_budgets(db_session, cabinet_id, [row], _T0) == 1
    later = _T0 + timedelta(minutes=5)
    assert await insert_budgets(db_session, cabinet_id, [{**row, "total": 120000}], later) == 1

    budgets = (
        await db_session.execute(
            select(Budget)
            .where(Budget.cabinet_id == cabinet_id)
            .order_by(Budget.captured_at.desc(), Budget.id.desc())
        )
    ).scalars().all()
    assert len(budgets) == 2
    latest = budgets[0]
    assert latest.captured_at == later
    assert (latest.cash_kopecks, latest.netting_kopecks, latest.total_kopecks) == (
        100000,
        50000,
        120000,
    )


async def test_insert_balance(db_session):
    cabinet_id = await _cabinet_id(db_session)

    assert await insert_balance(db_session, cabinet_id, 700000, _T0) == 1

    row = (await db_session.execute(select(Balance))).scalars().one()
    assert (row.cabinet_id, row.total_kopecks, row.captured_at) == (cabinet_id, 700000, _T0)


async def test_upsert_positions(db_session):
    cabinet_id = await _cabinet_id(db_session)
    row = {"advert_id": 1, "nm_id": 111, "date": date(2026, 10, 10), "avg_position": 3.4}

    assert await upsert_positions(db_session, cabinet_id, [row]) == 1
    assert await upsert_positions(db_session, cabinet_id, [{**row, "avg_position": 2.0}]) == 1

    rows = (await db_session.execute(select(Position))).scalars().all()
    assert len(rows) == 1
    assert rows[0].avg_position == Decimal("2.00")


async def test_upsert_clusters(db_session):
    cabinet_id = await _cabinet_id(db_session)
    row = {
        "norm_query": "кроссовки",
        "plus_type": "positive",
        "stats": {"orders": 3},
        "bid_kopecks": 15000,
    }

    assert await upsert_clusters(db_session, cabinet_id, 1001, [dict(row)]) == 1
    assert await upsert_clusters(db_session, cabinet_id, 1001, [{**row, "bid_kopecks": 18000}]) == 1

    rows = (await db_session.execute(select(Cluster))).scalars().all()
    assert len(rows) == 1  # same (cabinet, advert, norm_query) -> updated in place
    assert rows[0].bid_kopecks == 18000
    assert rows[0].stats == {"orders": 3}
    assert rows[0].updated_at is not None


async def test_insert_snapshot(db_session):
    cabinet_id = await _cabinet_id(db_session)
    payload = {"competitiveBid": 12000}
    parsed = {"nm_id": 111, "competitive_kopecks": 12000}

    assert await insert_snapshot(db_session, cabinet_id, 1001, payload, parsed, _T0) == 1

    row = (await db_session.execute(select(AuctionSnapshot))).scalars().one()
    assert row.payload == payload  # JSONB roundtrip
    assert row.parsed == parsed
    assert row.captured_at == _T0


async def test_log_collection_writes_rows(db_session):
    cabinet_id = await _cabinet_id(db_session)
    started = _T0
    finished = started + timedelta(seconds=42)

    assert await log_collection(db_session, None, "cleanup", "ok", None, started, finished) == 1
    assert (
        await log_collection(
            db_session, cabinet_id, "fullstats", "error", "403 scope is not allowed", started, finished
        )
        == 1
    )

    rows = (await db_session.execute(select(CollectionLog).order_by(CollectionLog.id))).scalars().all()
    assert [r.cabinet_id for r in rows] == [None, cabinet_id]  # cabinet is nullable
    assert rows[0].loop_name == "cleanup"
    assert rows[0].finished_at - rows[0].started_at == timedelta(seconds=42)
    assert rows[1].loop_name == "fullstats"
    assert rows[1].status == "error"
    assert rows[1].detail == "403 scope is not allowed"


# --- app_settings (get_setting / set_setting) ------------------------------------


async def test_get_setting_default_when_absent(db_session):
    from app.repos import get_setting

    assert await get_setting(db_session, "controller_mode", "dry_run") == "dry_run"
    assert await get_setting(db_session, "nope") is None


async def test_set_setting_upserts_scalars_and_objects(db_session):
    from app.repos import get_setting, set_setting

    # jsonb хранит и скаляры: kill-switch bool, coverage int, mode str
    await set_setting(db_session, "controller_kill_switch", True)
    await set_setting(db_session, "coverage_pct", 90)
    await set_setting(db_session, "controller_mode", "dry_run")
    assert await get_setting(db_session, "controller_kill_switch") is True
    assert await get_setting(db_session, "coverage_pct") == 90
    assert await get_setting(db_session, "controller_mode") == "dry_run"

    # повторная запись того же ключа — обновление, а не дубль (PK key)
    await set_setting(db_session, "controller_mode", "live")
    assert await get_setting(db_session, "controller_mode") == "live"

    # словарь тоже валидное jsonb-значение
    await set_setting(db_session, "extra", {"a": 1})
    assert await get_setting(db_session, "extra") == {"a": 1}
