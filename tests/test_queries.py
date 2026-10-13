"""Integration tests for dashboard queries (app/queries.py) on real Postgres.

Stats dates are Moscow-calendar dates (the collector stores them that way), so
the seeds and expectations are computed from msk_today() time-dependently.
Each test runs inside the db_session savepoint and is rolled back.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Cabinet
from app.queries import cabinet_campaigns, msk_today, overview
from tests.helpers import (
    make_balance,
    make_bid,
    make_cabinet,
    make_campaign,
    make_collection_log,
    make_stat,
)

# The engine/db_session fixtures live on the session-scoped event loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def test_overview_spends_balance_freshness(db_session):
    today = msk_today()
    yesterday = today - timedelta(days=1)
    d10 = today - timedelta(days=10)
    d40 = today - timedelta(days=40)

    cab_by = make_cabinet(name="Alpha BY", wb_type="BY", currency="BYN")
    cab_ru = make_cabinet(name="Beta RU", wb_type="RU", currency="RUB")
    cab_off = make_cabinet(name="Gamma off", wb_type="BY", currency="BYN", active=False)
    db_session.add_all([cab_by, cab_ru, cab_off])
    await db_session.flush()

    # Alpha: one product advertised on 4 dates -> each bucket is distinct.
    db_session.add_all(
        [
            make_stat(cab_by.id, 101, 1, today, sum_kopecks=1000),
            make_stat(cab_by.id, 101, 1, yesterday, sum_kopecks=2000),
            make_stat(cab_by.id, 101, 1, d10, sum_kopecks=4000),
            make_stat(cab_by.id, 101, 1, d40, sum_kopecks=8000),
            # Beta: today only, plus an old row that must not leak into buckets.
            make_stat(cab_ru.id, 201, 5, today, sum_kopecks=500),
            make_stat(cab_ru.id, 201, 5, d40, sum_kopecks=999),
        ]
    )
    # Alpha balance: two observations -> the latest (by captured_at) wins.
    db_session.add_all(
        [
            make_balance(cab_by.id, 10000, _days_ago(2)),
            make_balance(cab_by.id, 15000, _days_ago(1)),
        ]
    )
    # Freshness: max(finished_at) of "ok" collection_log rows; errors don't count.
    ok_newer = _days_ago(1)
    ok_older = _days_ago(2)
    db_session.add_all(
        [
            make_collection_log(cab_by.id, finished_at=ok_older),
            make_collection_log(cab_by.id, finished_at=ok_newer),
            make_collection_log(cab_ru.id, status="error", finished_at=_days_ago(1)),
            make_collection_log(cab_off.id, finished_at=_days_ago(1)),
        ]
    )
    await db_session.commit()

    rows = await overview(db_session)
    by_name = {row["cabinet"].name: row for row in rows}

    # Inactive cabinets are not listed at all.
    assert sorted(by_name) == ["Alpha BY", "Beta RU"]

    alpha = by_name["Alpha BY"]
    assert alpha["cabinet"].currency == "BYN"
    assert alpha["spent_today"] == 1000
    assert alpha["spent_7d"] == 3000  # today + yesterday
    assert alpha["spent_30d"] == 7000  # + 10 days ago
    assert alpha["balance"] == 15000
    assert alpha["freshness"] == ok_newer

    beta = by_name["Beta RU"]
    assert beta["cabinet"].currency == "RUB"
    assert beta["spent_today"] == 500
    assert beta["spent_7d"] == 500
    assert beta["spent_30d"] == 500
    assert beta["balance"] is None  # no balance rows
    assert beta["freshness"] is None  # only failed collection runs


async def test_overview_empty_db(db_session):
    assert await overview(db_session) == []


async def test_cabinet_campaigns_with_latest_bids(db_session):
    cab = make_cabinet(name="Cab", wb_type="BY", currency="BYN")
    db_session.add(cab)
    await db_session.flush()

    db_session.add_all(
        [
            make_campaign(cab.id, 300, name="Кампания C", type=8, status=9,
                          payment_type="cpc", bid_type="auto",
                          placement_search=True, placement_reco=True),
            make_campaign(cab.id, 100, name="Кампания A", type=1, status=11,
                          payment_type="cpc", bid_type="manual",
                          placement_search=True, placement_reco=False),
            make_campaign(cab.id, 200, name="Кампания B", type=5, status=7,
                          payment_type="cpm", bid_type="auto",
                          placement_search=False, placement_reco=True),
        ]
    )
    # Latest bid per (advert, placement) by captured_at: advert 300/search saw
    # two observations, the newer (700) must win over 500.
    db_session.add_all(
        [
            make_bid(cab.id, 300, 11, "search", 500, _days_ago(1)),
            make_bid(cab.id, 300, 11, "search", 700, _days_ago(0.5)),
            make_bid(cab.id, 300, 11, "recommendations", 300, _days_ago(0.75)),
            make_bid(cab.id, 100, 12, "search", 150, _days_ago(1)),
        ]
    )
    await db_session.commit()

    rows = await cabinet_campaigns(db_session, cab.id, None)
    assert [row["advert_id"] for row in rows] == [100, 200, 300]  # sorted by advert_id

    c = rows[2]  # advert 300
    assert c["name"] == "Кампания C"
    assert c["type"] == 8
    assert c["status"] == 9
    assert c["payment_type"] == "cpc"
    assert c["bid_type"] == "auto"
    assert c["placement_search"] is True
    assert c["placement_reco"] is True
    assert c["bid_search"] == 700
    assert c["bid_reco"] == 300

    assert rows[0]["bid_search"] == 150
    assert rows[0]["bid_reco"] is None
    assert rows[1]["bid_search"] is None  # no bids at all -> None
    assert rows[1]["bid_reco"] is None


async def test_cabinet_campaigns_status_filter(db_session):
    cab = make_cabinet(name="Cab2", wb_type="RU", currency="RUB")
    other = make_cabinet(name="Cab2 other")
    db_session.add_all([cab, other])
    await db_session.flush()
    db_session.add_all(
        [
            make_campaign(cab.id, 1, status=9),
            make_campaign(cab.id, 2, status=11),
            make_campaign(cab.id, 3, status=7),
            make_campaign(cab.id, 4, status=-1),
            # another cabinet's status-9 campaign must never leak into the filter
            make_campaign(other.id, 1, status=9),
        ]
    )
    await db_session.commit()

    assert [r["advert_id"] for r in await cabinet_campaigns(db_session, cab.id, "active")] == [1]
    assert [r["advert_id"] for r in await cabinet_campaigns(db_session, cab.id, "paused")] == [2]
    assert [r["advert_id"] for r in await cabinet_campaigns(db_session, cab.id, "completed")] == [3]
    assert [r["advert_id"] for r in await cabinet_campaigns(db_session, cab.id, "done")] == []
    assert [r["advert_id"] for r in await cabinet_campaigns(db_session, cab.id, "deleted")] == [4]
    assert len(await cabinet_campaigns(db_session, cab.id, None)) == 4


async def test_cabinet_campaigns_unknown_status_raises(db_session):
    with pytest.raises(ValueError):
        await cabinet_campaigns(db_session, 12345, "bogus")


async def test_cabinet_isolation(db_session):
    """Campaigns and bids of another cabinet are never returned."""
    cab1 = make_cabinet(name="Iso 1")
    cab2 = make_cabinet(name="Iso 2")
    db_session.add_all([cab1, cab2])
    await db_session.flush()
    db_session.add_all(
        [
            make_campaign(cab1.id, 10, name=" чужая "),
            make_bid(cab2.id, 10, 1, "search", 999, _days_ago(1)),
        ]
    )
    await db_session.commit()

    rows = await cabinet_campaigns(db_session, cab2.id, None)
    assert rows == []  # campaign belongs to cab1

    rows = await cabinet_campaigns(db_session, cab1.id, None)
    assert len(rows) == 1
    assert rows[0]["bid_search"] is None  # the bid was captured for cab2


async def test_make_cabinet_encrypts_token():
    from app.crypto import decrypt_token

    cab = make_cabinet(token="plain-secret")
    assert cab.token_encrypted != "plain-secret"
    assert decrypt_token(cab.token_encrypted) == "plain-secret"
    assert isinstance(cab, Cabinet)
