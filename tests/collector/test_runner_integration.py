"""Runner integration tests: fake WB client + real dev-Postgres.

The FakeClient stubs every WbClient read method the runner uses; collect_*
functions take the client as their first argument, so the fake is passed in
directly. Sessions come from the db_session fixture (per-test savepoint, so
all writes roll back at teardown).
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.runner import (
    LOOPS_SPEC,
    cleanup_raw_responses,
    collect_auction_snapshot,
    collect_balance,
    collect_bids_min,
    collect_budgets,
    collect_campaigns,
    collect_clusters,
    collect_fullstats,
    save_raw,
)
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
    RawResponse,
    Stat,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_MSK = timezone(timedelta(hours=3))


def _load(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    """In-memory WbClient stand-in recording every call the runner makes."""

    def __init__(self) -> None:
        self.adverts_calls: list[list[int]] = []
        self.budgets_calls: list[list[int]] = []
        self.fullstats_calls: list[tuple[int, date, date]] = []
        self.bids_min_calls: list[list[int]] = []
        self.recommendations_calls: list[dict] = []
        self.normquery_list_calls: list[int] = []
        self.normquery_stats_calls: list[dict] = []

    async def promotion_count(self) -> dict:
        # all statuses, not only active: pauses/finishings must stay visible
        return {
            "all": 3,
            "adverts": [
                {
                    "type": 8,
                    "chards": [
                        {"status": 9, "count": 2, "advertList": [1001, 1002]},
                        {"status": 11, "count": 1, "advertList": [1003]},
                    ],
                }
            ],
        }

    async def adverts(self, ids: list[int]) -> list[dict]:
        self.adverts_calls.append(list(ids))
        return _load("adverts_sample.json")

    async def budgets(self, advert_ids: list[int]) -> list[dict]:
        self.budgets_calls.append(list(advert_ids))
        return _load("budget_sample.json")

    async def balance(self) -> dict:
        return {"total": 12345}

    async def fullstats(self, advert_ids: list[int], begin: date, end: date) -> list[dict]:
        self.fullstats_calls.append((advert_ids[0], begin, end))
        return _load("fullstats_sample.json")

    async def bids_min(self, nm_ids: list[int]) -> dict:
        self.bids_min_calls.append(list(nm_ids))
        return {"nmIDs": nm_ids, "items": []}

    async def recommendations(self, params: dict) -> dict:
        self.recommendations_calls.append(dict(params))
        return {"advertId": params.get("advertId"), "prices": []}

    async def normquery_list(self, advert_id: int) -> dict:
        self.normquery_list_calls.append(advert_id)
        return {"data": [{"phrase": "кроссовки"}, {"phrase": "кроссовки белые"}]}

    async def normquery_stats(self, body: dict) -> dict:
        self.normquery_stats_calls.append(dict(body))
        return {"data": [{"phrase": q} for q in body.get("queries", [])]}


async def _cabinet_id(db_session: AsyncSession) -> int:
    cabinet = Cabinet(
        name="runner-test", wb_type="BY", currency="BYN",
        token_encrypted=encrypt_token("tok"),
    )
    db_session.add(cabinet)
    await db_session.commit()
    return cabinet.id


async def _count(db_session: AsyncSession, model, *conditions) -> int:
    stmt = select(func.count()).select_from(model)
    if conditions:
        stmt = stmt.where(*conditions)
    return int((await db_session.execute(stmt)).scalar_one())


def _session_factory(session: AsyncSession):
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


async def test_loops_spec_periods() -> None:
    periods = {spec.name: spec.period_sec for spec in LOOPS_SPEC}
    assert periods == {
        "campaigns": 300,
        "budgets": 300,
        "balance": 300,
        "fullstats": 1200,
        "bids_min": 86400,
        "auction_snapshot": 3600,
        "clusters": 86400,
    }


async def test_save_raw_writes_and_respects_zero_retention(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.collector import runner

    cab = await _cabinet_id(db_session)
    await save_raw(db_session, cab, "/adv/v1/balance", 200, {"total": 1})
    assert await _count(db_session, RawResponse) == 1

    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(raw_log_days=0))
    await save_raw(db_session, cab, "/adv/v1/balance", 200, {"total": 2})
    assert await _count(db_session, RawResponse) == 1  # nothing written


async def test_full_pipeline_writes_all_tables(db_session: AsyncSession) -> None:
    cab = await _cabinet_id(db_session)
    client = FakeClient()

    # campaigns: count -> ids (ALL statuses) -> adverts -> upsert + bid history
    n = await collect_campaigns(client, db_session, cab)
    assert n == 3  # three campaign rows processed
    assert await _count(db_session, Campaign) == 3
    assert await _count(db_session, BidHistory) == 3  # 1001/111 + 1003/333 x2 placements
    bids = (
        (await db_session.execute(select(BidHistory).order_by(BidHistory.id)))
        .scalars()
        .all()
    )
    assert {(b.advert_id, b.nm_id, b.placement, b.bid_kopecks) for b in bids} == {
        (1001, 111, "search", 25000),
        (1003, 333, "search", 21000),
        (1003, 333, "recommendations", 15000),
    }
    # the paused/finished campaign (status 11) is stored too
    statuses = {
        row.advert_id: row.status
        for row in (await db_session.execute(select(Campaign))).scalars()
    }
    assert statuses == {1001: 9, 1002: 9, 1003: 11}

    # budgets: only active campaigns (status 9/11) from the DB, chunks <= 50
    n = await collect_budgets(client, db_session, cab)
    assert n == 1  # one budget row from the sample
    assert client.budgets_calls == [[1001, 1002, 1003]]  # all in one <=50 chunk
    budget = (await db_session.execute(select(Budget))).scalar_one()
    assert (budget.advert_id, budget.total_kopecks) == (1, 15000000)  # 150000 * 100 (kopecks)

    # balance
    n = await collect_balance(client, db_session, cab)
    assert n == 1
    balance = (await db_session.execute(select(Balance))).scalar_one()
    assert balance.total_kopecks == 12345

    # fullstats: one call per active campaign, oldest-success-first order
    n = await collect_fullstats(client, db_session, cab)
    assert n == 18  # per call: 4 stat rows + 2 position rows; 3 campaigns, 4+2 upserts counted once (same sample)
    assert [call[0] for call in client.fullstats_calls] == [1001, 1002, 1003]
    today = datetime.now(_MSK).date()
    assert all(
        begin == today - timedelta(days=30) and end == today
        for _, begin, end in client.fullstats_calls
    )  # no prior success -> full 31-day window (retro history)
    assert await _count(db_session, Stat) == 4
    assert await _count(db_session, Position) == 2
    logs = (
        (
            await db_session.execute(
                select(CollectionLog)
                .where(CollectionLog.cabinet_id == cab)
                .order_by(CollectionLog.id)
            )
        )
        .scalars()
        .all()
    )
    fullstats_logs = [row for row in logs if row.loop_name.startswith("fullstats:")]
    assert [row.loop_name for row in fullstats_logs] == [
        "fullstats:1001", "fullstats:1002", "fullstats:1003",
    ]
    assert all(row.status == "ok" for row in fullstats_logs)

    # second fullstats pass: window resumes from the last success (today)
    await collect_fullstats(client, db_session, cab)
    assert client.fullstats_calls[3][1:] == (today, today)

    # bids_min: nm ids from the existing bid history, snapshot stored raw
    n = await collect_bids_min(client, db_session, cab)
    assert n == 1
    assert client.bids_min_calls == [[111, 333]]
    snaps = (await db_session.execute(select(AuctionSnapshot))).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].parsed == {"kind": "bids_min"}
    assert snaps[0].payload == {"nmIDs": [111, 333], "items": []}

    # auction snapshots: only manual+cpm active campaigns (1002 cpc/unified skipped)
    n = await collect_auction_snapshot(client, db_session, cab)
    assert n == 2
    assert client.recommendations_calls == [
        {"advertId": 1001, "type": 8, "payment_type": "cpm"},
        {"advertId": 1003, "type": 8, "payment_type": "cpm"},
    ]
    snaps = (
        (await db_session.execute(select(AuctionSnapshot).order_by(AuctionSnapshot.id)))
        .scalars()
        .all()
    )
    assert [s.parsed for s in snaps] == [
        {"kind": "bids_min"}, {"kind": "recommendations"}, {"kind": "recommendations"},
    ]

    # clusters: manual campaigns only, phrases -> stats, raw stats jsonb kept
    n = await collect_clusters(client, db_session, cab)
    assert n == 4  # 2 phrases x 2 manual campaigns
    assert client.normquery_list_calls == [1001, 1003]
    assert client.normquery_stats_calls == [
        {"advertId": 1001, "queries": ["кроссовки", "кроссовки белые"]},
        {"advertId": 1003, "queries": ["кроссовки", "кроссовки белые"]},
    ]
    clusters = (await db_session.execute(select(Cluster))).scalars().all()
    assert len(clusters) == 4
    assert {c.norm_query for c in clusters} == {"кроссовки", "кроссовки белые"}
    # the raw stats response is kept as jsonb per cluster (format unconfirmed)
    raw_stats = {"data": [{"phrase": "кроссовки"}, {"phrase": "кроссовки белые"}]}
    assert all(c.stats == raw_stats for c in clusters)

    # raw responses: every endpoint hit is journaled
    endpoints = {
        row.endpoint
        for row in (await db_session.execute(select(RawResponse))).scalars()
    }
    assert endpoints == {
        "/adv/v1/promotion/count",
        "/api/advert/v2/adverts",
        "/api/advert/v2/budget",
        "/adv/v1/balance",
        "/adv/v3/fullstats",
        "/api/advert/v1/bids/min",
        "/api/advert/v0/bids/recommendations",
        "/adv/v0/normquery/list",
        "/adv/v1/normquery/stats",
    }
    # count + adverts + budget + balance + fullstats x2 passes
    # + bids_min + recommendations x2 + normquery list x2 + normquery stats x2
    assert await _count(db_session, RawResponse) == 1 + 1 + 1 + 1 + 3 + 3 + 1 + 2 + 2 + 2


async def test_bids_min_skipped_without_bid_history(db_session: AsyncSession) -> None:
    cab = await _cabinet_id(db_session)
    client = FakeClient()
    n = await collect_bids_min(client, db_session, cab)
    assert n == 0
    assert client.bids_min_calls == []


async def test_fullstats_continues_after_per_campaign_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WbApiError on one campaign is logged "error" and the rest go on."""
    from app.wb.errors import WbApiError

    cab = await _cabinet_id(db_session)
    db_session.add_all(
        [
            Campaign(
                cabinet_id=cab, advert_id=2001, name="ok-1", type=8, status=9,
                payment_type="cpm", bid_type="manual",
            ),
            Campaign(
                cabinet_id=cab, advert_id=2002, name="broken", type=8, status=9,
                payment_type="cpm", bid_type="manual",
            ),
            Campaign(
                cabinet_id=cab, advert_id=2003, name="ok-2", type=8, status=9,
                payment_type="cpm", bid_type="manual",
            ),
        ]
    )
    await db_session.commit()

    client = FakeClient()
    real_fullstats = client.fullstats

    async def flaky(advert_ids: list[int], begin: date, end: date) -> list[dict]:
        if advert_ids[0] == 2002:
            raise WbApiError(500, "wb down")
        return await real_fullstats(advert_ids, begin, end)

    client.fullstats = flaky  # type: ignore[method-assign]
    n = await collect_fullstats(client, db_session, cab)
    assert [call[0] for call in client.fullstats_calls] == [2001, 2003]
    assert n == 12  # 2 campaigns x (4 stats + 2 positions)
    logs = (
        (
            await db_session.execute(
                select(CollectionLog)
                .where(CollectionLog.cabinet_id == cab)
                .order_by(CollectionLog.id)
            )
        )
        .scalars()
        .all()
    )
    by_loop = {row.loop_name: row.status for row in logs}
    assert by_loop == {
        "fullstats:2001": "ok",
        "fullstats:2002": "error",
        "fullstats:2003": "ok",
    }


async def test_auction_snapshot_4xx_logged_and_skipped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.wb.errors import WbApiError

    cab = await _cabinet_id(db_session)
    db_session.add_all(
        [
            Campaign(
                cabinet_id=cab, advert_id=3001, name="good", type=8, status=9,
                payment_type="cpm", bid_type="manual",
            ),
            Campaign(
                cabinet_id=cab, advert_id=3002, name="forbidden", type=8, status=9,
                payment_type="cpm", bid_type="manual",
            ),
        ]
    )
    await db_session.commit()

    client = FakeClient()

    async def recommendations(params: dict) -> dict:
        if params["advertId"] == 3002:
            raise WbApiError(403, "nope")
        return {"advertId": params["advertId"]}

    client.recommendations = recommendations  # type: ignore[method-assign]
    n = await collect_auction_snapshot(client, db_session, cab)
    assert n == 1  # the 4xx campaign is skipped, the loop went on
    logs = (
        (
            await db_session.execute(
                select(CollectionLog)
                .where(CollectionLog.cabinet_id == cab)
                .order_by(CollectionLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.loop_name, row.status) for row in logs] == [
        ("auction_snapshot:3002", "error"),
    ]
    snaps = (await db_session.execute(select(AuctionSnapshot))).scalars().all()
    assert [s.advert_id for s in snaps] == [3001]


async def test_cleanup_raw_responses_deletes_only_old_rows(
    db_session: AsyncSession,
) -> None:
    cab = await _cabinet_id(db_session)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            RawResponse(
                cabinet_id=cab, endpoint="/old", status=200, payload={},
                captured_at=now - timedelta(days=20),
            ),
            RawResponse(
                cabinet_id=cab, endpoint="/fresh", status=200, payload={},
                captured_at=now - timedelta(days=1),
            ),
        ]
    )
    await db_session.commit()
    deleted = await cleanup_raw_responses(_session_factory(db_session), keep_days=14)
    assert deleted == 1
    remaining = {
        row.endpoint
        for row in (await db_session.execute(select(RawResponse))).scalars()
    }
    assert remaining == {"/fresh"}


async def test_save_raw_used_for_all_endpoints() -> None:
    """Every runner save_raw call site targets a journaled WB endpoint."""
    # smoke guard: keep the endpoint labels aligned with app/wb/constants.klass
    from app.wb.constants import klass

    for endpoint in (
        "/adv/v1/promotion/count",
        "/api/advert/v2/adverts",
        "/api/advert/v2/budget",
        "/adv/v1/balance",
        "/adv/v3/fullstats",
        "/api/advert/v1/bids/min",
        "/api/advert/v0/bids/recommendations",
        "/adv/v0/normquery/list",
        "/adv/v1/normquery/stats",
    ):
        assert klass(endpoint)  # all paths have a limit class
