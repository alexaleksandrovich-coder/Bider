"""Integration tests for detail queries (Task 9): campaign_detail,
items_summary, health_status — on real Postgres, seeded via tests.helpers.

Seed shape (campaign A = advert 101, campaign B = advert 202):
- A stats: 5 dates (today..today-10), 2 nm x 2 app_types per date, so every
  total is aggregated across both dimensions; only 7/30-day windows are
  legal, and today-10 must stay outside the days=7 window while today-3
  belongs to it.
- bids: 3 search points + 1 recommendations point; positions: 2 nms on one
  date; budgets: 2 captures (latest wins); clusters: 2 phrases;
  auction_snapshots: 3 captures (page shows the last 5 at most).

Each test runs inside the db_session savepoint and is rolled back.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import AuctionSnapshot, Budget, Cluster, Position
from app.queries import campaign_detail, health_status, items_summary, msk_today
from tests.helpers import (
    make_bid,
    make_cabinet,
    make_campaign,
    make_collection_log,
    make_stat,
)

# The engine/db_session fixtures live on the session-scoped event loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")

_NM_A1, _NM_A2, _NM_B = 111, 222, 333


def _days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def _seed_campaign_a(db):
    """Cabinet + campaign A (101) + campaign B (202) with all detail tables.

    Per (date, nm, app_type) row values:
      nm 111: views=100 clicks=10 orders=1 shks=1 sum=1500 sum_price=20000
      nm 222: views=100 clicks=10 orders=1 shks=1 sum=500  sum_price=5000
    -> per date (2 app_types): views 400, clicks 40, orders 4, shks 4,
       sum 4000, sum_price 50000.

    Returns (cabinet, seeds); seeds holds the exact seeded timestamps
    (bid captures, snapshot captures) so tests avoid datetime.now() drift.
    """
    today = msk_today()
    seeds: dict = {}
    cab = make_cabinet(name="T9 Detail", wb_type="BY", currency="BYN")
    db.add(cab)
    await db.flush()
    db.add_all(
        [
            make_campaign(cab.id, 101, name="РК Аукцион", type=8, status=9,
                          payment_type="cpm", bid_type="manual",
                          placement_search=True, placement_reco=True),
            make_campaign(cab.id, 202, name="РК Прочая", type=1, status=9,
                          payment_type="cpc", bid_type="auto"),
        ]
    )
    for offset in (0, 1, 2, 3, 10):  # 0/1/2 in the 3-day window, 3 in the 7-day
        day = today - timedelta(days=offset)
        for nm, sum_k, sum_p in ((_NM_A1, 1500, 20000), (_NM_A2, 500, 5000)):
            for app_type in ("android", "ios"):
                db.add(
                    make_stat(
                        cab.id, 101, nm, day, app_type=app_type,
                        views=100, clicks=10, orders=1, shks=1,
                        sum_kopecks=sum_k, sum_price_kopecks=sum_p,
                    )
                )
    # campaign B: one stat with zero revenue (drr must be None in items_summary)
    db.add(
        make_stat(cab.id, 202, _NM_B, today, views=10, clicks=2,
                  orders=2, shks=3, sum_kopecks=700, sum_price_kopecks=0)
    )
    # bids: 3 search + 1 recommendations, timestamps ordered
    bid_ts = [_days_ago(3 / 24), _days_ago(2 / 24), _days_ago(1 / 24), _days_ago(0.5 / 24)]
    seeds["bid_ts"] = bid_ts
    db.add_all(
        [
            make_bid(cab.id, 101, _NM_A1, "search", 400, bid_ts[0]),
            make_bid(cab.id, 101, _NM_A1, "search", 500, bid_ts[1]),
            make_bid(cab.id, 101, _NM_A2, "search", 600, bid_ts[2]),
            make_bid(cab.id, 101, _NM_A1, "recommendations", 300, bid_ts[3]),
        ]
    )
    # positions: two products on one date -> per-date average 4.0
    db.add_all(
        [
            Position(cabinet_id=cab.id, advert_id=101, nm_id=_NM_A1,
                     date=today - timedelta(days=1), avg_position=3.50),
            Position(cabinet_id=cab.id, advert_id=101, nm_id=_NM_A2,
                     date=today - timedelta(days=1), avg_position=4.50),
        ]
    )
    # budgets: two captures -> the latest (by captured_at) is current
    db.add_all(
        [
            Budget(cabinet_id=cab.id, advert_id=101, cash_kopecks=10000,
                   netting_kopecks=0, total_kopecks=50000, captured_at=_days_ago(2 / 24)),
            Budget(cabinet_id=cab.id, advert_id=101, cash_kopecks=8000,
                   netting_kopecks=500, total_kopecks=45000, captured_at=_days_ago(1 / 24)),
        ]
    )
    # clusters: two manual-bid phrases
    db.add_all(
        [
            Cluster(cabinet_id=cab.id, advert_id=101, norm_query="кроссовки",
                    plus_type="фраза", bid_kopecks=250000, updated_at=_days_ago(1)),
            Cluster(cabinet_id=cab.id, advert_id=101, norm_query="кроссовки летние",
                    plus_type="фраза", bid_kopecks=300000, updated_at=_days_ago(2)),
        ]
    )
    # auction snapshots: three captures, newest last
    snap_ts = {i: _days_ago(age) for i, age in ((1, 3 / 24), (2, 2 / 24), (3, 1 / 24))}
    seeds["snap_ts"] = snap_ts
    for i, captured_at in snap_ts.items():
        db.add(
            AuctionSnapshot(cabinet_id=cab.id, advert_id=101,
                            payload={"i": i}, parsed={"kind": "recommendations"},
                            captured_at=captured_at)
        )
    await db.commit()
    return cab, seeds


# --- campaign_detail ---------------------------------------------------------


async def test_campaign_detail_totals_window_and_aggregation(db_session):
    cab, _seeds = await _seed_campaign_a(db_session)

    d = await campaign_detail(db_session, cab.id, 101, days=7)
    assert d["campaign"] is not None
    assert d["campaign"].advert_id == 101

    t = d["totals"]
    # 4 dates in window (today..today-3) x 2 nm x 2 app_types; today-10 is
    # beyond the window boundary and must not get in.
    assert t["views"] == 1600
    assert t["clicks"] == 160
    assert t["orders"] == 16
    assert t["shks"] == 16
    assert t["sum_kopecks"] == 16000
    assert t["sum_price_kopecks"] == 200000

    # 30-day window includes every seeded date (today..today-10).
    d30 = await campaign_detail(db_session, cab.id, 101, days=30)
    assert d30["totals"]["sum_kopecks"] == 20000
    assert d30["totals"]["views"] == 2000
    assert d30["totals"]["sum_price_kopecks"] == 250000

    # campaign B's stats never leak into A's detail.
    d202 = await campaign_detail(db_session, cab.id, 202, days=7)
    assert d202["totals"]["sum_kopecks"] == 700
    assert d202["totals"]["sum_price_kopecks"] == 0


async def test_campaign_detail_per_nm_sorted_by_spend(db_session):
    cab, _seeds = await _seed_campaign_a(db_session)

    d = await campaign_detail(db_session, cab.id, 101, days=7)
    rows = d["per_nm"]
    assert [r["nm_id"] for r in rows] == [_NM_A1, _NM_A2]  # sum_kopecks desc

    top = rows[0]  # 4 dates x 2 app_types of the nm-111 values
    assert top["views"] == 800
    assert top["clicks"] == 80
    assert top["orders"] == 8
    assert top["shks"] == 8
    assert top["sum_kopecks"] == 12000
    assert top["sum_price_kopecks"] == 160000
    assert top["drr"] == "7.5%"
    assert rows[1]["sum_kopecks"] == 4000
    assert rows[1]["drr"] == "10.0%"


async def test_campaign_detail_series_budget_clusters_snapshots(db_session):
    cab, seeds = await _seed_campaign_a(db_session)

    d = await campaign_detail(db_session, cab.id, 101, days=7)

    # bid_series: chronological, ts is an ISO string.
    bids = d["bid_series"]
    assert [(b["placement"], b["bid_kopecks"]) for b in bids] == [
        ("search", 400),
        ("search", 500),
        ("search", 600),
        ("recommendations", 300),
    ]
    assert bids[0]["ts"] == seeds["bid_ts"][0].isoformat()
    assert bids[-1]["ts"] >= bids[0]["ts"]

    # position_series: per-date average across nms.
    assert d["position_series"] == [
        {"date": (msk_today() - timedelta(days=1)).isoformat(), "avg": 4.0}
    ]

    # spend_series: one entry per day of the window, no holes; days without
    # data -> sum_kopecks None. Window days=7: today-6 has no stats.
    spend = d["spend_series"]
    assert len(spend) == 7
    assert spend[0]["date"] == (msk_today() - timedelta(days=6)).isoformat()
    assert spend[0]["sum_kopecks"] is None
    by_date = {row["date"]: row["sum_kopecks"] for row in spend}
    assert by_date[(msk_today() - timedelta(days=3)).isoformat()] == 4000
    assert by_date[msk_today().isoformat()] == 4000

    # budget: latest capture wins.
    assert d["budget"] == {"cash": 8000, "netting": 500, "total": 45000}

    # clusters: sorted by phrase.
    assert [c["norm_query"] for c in d["clusters"]] == ["кроссовки", "кроссовки летние"]
    assert d["clusters"][0]["bid_kopecks"] == 250000
    assert d["clusters"][1]["bid_kopecks"] == 300000

    # snapshots_last: newest first, at most 5 (3 seeded).
    snaps = d["snapshots_last"]
    assert [s["payload"]["i"] for s in snaps] == [3, 2, 1]


async def test_campaign_detail_unknown_campaign(db_session):
    cab, _seeds = await _seed_campaign_a(db_session)
    d = await campaign_detail(db_session, cab.id, 999999, days=7)
    assert d["campaign"] is None
    assert d["totals"]["views"] == 0


async def test_campaign_detail_bad_days_raises(db_session):
    cab, _seeds = await _seed_campaign_a(db_session)
    for bad in (1, 3, 13, 0, -7):
        with pytest.raises(ValueError):
            await campaign_detail(db_session, cab.id, 101, days=bad)


# --- items_summary -----------------------------------------------------------


async def test_items_summary_aggregates_all_campaigns(db_session):
    cab, _seeds = await _seed_campaign_a(db_session)

    rows = await items_summary(db_session, cab.id)
    # sorted by spent desc; every campaign of the cabinet contributes
    assert [r["nm_id"] for r in rows] == [_NM_A1, _NM_A2, _NM_B]

    a1 = rows[0]  # 5 dates x 2 app_types of the nm-111 values
    assert a1["spent"] == 15000
    assert a1["revenue"] == 200000
    assert a1["orders"] == 10
    assert a1["shks"] == 10
    assert a1["drr"] == "7.5%"

    a2 = rows[1]
    assert a2["spent"] == 5000
    assert a2["revenue"] == 50000
    assert a2["drr"] == "10.0%"

    # no revenue -> drr None
    assert rows[2]["spent"] == 700
    assert rows[2]["revenue"] == 0
    assert rows[2]["drr"] is None


async def test_items_summary_other_cabinet_isolated(db_session):
    cab1 = make_cabinet(name="Items 1")
    cab2 = make_cabinet(name="Items 2")
    db_session.add_all([cab1, cab2])
    await db_session.flush()
    db_session.add_all(
        [
            make_stat(cab1.id, 1, 50, msk_today(), sum_kopecks=100),
            make_stat(cab2.id, 2, 60, msk_today(), sum_kopecks=999),
        ]
    )
    await db_session.commit()

    rows = await items_summary(db_session, cab1.id)
    assert [r["nm_id"] for r in rows] == [50]
    assert rows[0]["spent"] == 100


async def test_items_summary_empty(db_session):
    cab = make_cabinet(name="Items empty")
    db_session.add(cab)
    await db_session.commit()
    assert await items_summary(db_session, cab.id) == []


# --- health_status -----------------------------------------------------------


async def test_health_status_groups_per_campaign_loops(db_session):
    cab1 = make_cabinet(name="Health 1")
    cab2 = make_cabinet(name="Health 2")
    db_session.add_all([cab1, cab2])
    await db_session.flush()
    fullstats_last = _days_ago(1 / 1440)
    db_session.add_all(
        [
            make_collection_log(cab1.id, loop_name="campaigns", status="ok",
                                detail="rows=5", finished_at=_days_ago(5 / 1440)),
            make_collection_log(cab1.id, loop_name="budgets", status="error",
                                detail="429", finished_at=_days_ago(10 / 1440)),
            # per-campaign fullstats rows must collapse into one "fullstats" loop
            make_collection_log(cab1.id, loop_name="fullstats:123", status="ok",
                                detail="stats=9", finished_at=_days_ago(40 / 1440)),
            make_collection_log(cab1.id, loop_name="fullstats:456", status="ok",
                                detail="stats=3", finished_at=_days_ago(2 / 1440)),
            make_collection_log(cab1.id, loop_name="fullstats:123", status="error",
                                detail="timeout", finished_at=fullstats_last),
            make_collection_log(cab2.id, loop_name="campaigns", status="ok",
                                finished_at=_days_ago(3 / 1440)),
            # cabinetless loop (nightly cleaner)
            make_collection_log(None, loop_name="cleanup", status="ok",
                                finished_at=_days_ago(30 / 1440)),
        ]
    )
    await db_session.commit()

    status = await health_status(db_session)
    loops = {(row["cabinet_id"], row["loop_name"]): row for row in status["loops"]}

    full = loops[(cab1.id, "fullstats")]
    assert full["loop_name"] == "fullstats"  # suffix ":<advert_id>" stripped
    assert full["last_status"] == "error"  # latest run by finished_at
    assert full["detail"] == "timeout"
    assert full["last_finished_at"] == fullstats_last

    assert loops[(cab1.id, "campaigns")]["last_status"] == "ok"
    assert loops[(cab1.id, "budgets")]["last_status"] == "error"
    assert loops[(cab2.id, "campaigns")]["last_status"] == "ok"
    assert loops[(None, "cleanup")]["last_status"] == "ok"
    assert len(loops) == 5


async def test_health_status_log_last_100(db_session):
    cab = make_cabinet(name="Health log")
    db_session.add(cab)
    await db_session.flush()
    for i in range(105):
        db_session.add(
            make_collection_log(cab.id, loop_name="campaigns", status="ok",
                                detail=f"run-{i}", finished_at=_days_ago(i / 1440))
        )
    await db_session.commit()

    status = await health_status(db_session)
    log = status["log"]
    assert len(log) == 100
    # newest first: run-104 was inserted last (highest id)
    assert log[0].detail == "run-104"
    details = {row.detail for row in log}
    assert "run-104" in details
    assert "run-0" not in details  # oldest rows fell out of the last-100 window
