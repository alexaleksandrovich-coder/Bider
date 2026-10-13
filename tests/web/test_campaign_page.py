"""Task 9 page tests: campaign detail, items summary, health.

Seeding follows tests/web/test_pages.py: the web routes persist through
app.db.SessionLocal (the bider_test database), so rows go in through a
throwaway engine before the request and the seeded cabinet is deleted
afterwards (FK ON DELETE CASCADE cleans children).
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AuctionSnapshot, Budget, Cabinet
from app.queries import msk_today
from tests.helpers import (
    make_bid,
    make_cabinet,
    make_campaign,
    make_collection_log,
    make_stat,
)

_T0 = datetime.now(UTC)
_ADVERT = 4001
_NM_BIG, _NM_SMALL = 555000111, 555000222


def _run_db(coro) -> object:
    """Run a `async def coro(db)` against a throwaway engine, return its result."""

    async def _with_engine():
        engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await coro(db)
        finally:
            await engine.dispose()

    return asyncio.run(_with_engine())


def _seed(cabinet, *children) -> int:
    """Insert the cabinet (+ children, cabinet_id patched) into the app DB."""

    async def _work(db):
        db.add(cabinet)
        await db.flush()
        for child in children:
            child.cabinet_id = cabinet.id
            db.add(child)
        await db.commit()
        return cabinet.id

    return _run_db(_work)


@pytest.fixture
def cabinet_ids() -> list[int]:
    """Collects cabinet ids a test created; deletes them (cascade) afterwards."""
    ids: list[int] = []

    async def _cleanup(db):
        if ids:
            await db.execute(delete(Cabinet).where(Cabinet.id.in_(ids)))
            await db.commit()

    yield ids
    _run_db(_cleanup)


def _seed_detail_cabinet(cabinet_ids: list[int]) -> int:
    """Cabinet + one CPC campaign with stats, budget, a bid and a snapshot.

    nm BIG: spend 1234.56 BYN / revenue 9999.99 BYN (drr 12.3%);
    nm SMALL: 10.00 BYN spent yesterday.
    """
    today = msk_today()
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T9 Page", wb_type="BY", currency="BYN"),
            make_campaign(None, _ADVERT, name="РК Детально", type=8, status=9,
                          payment_type="cpc", bid_type="auto",
                          placement_search=True, placement_reco=False),
            make_stat(None, _ADVERT, _NM_BIG, today,
                      views=1000, clicks=100, orders=10, shks=9,
                      sum_kopecks=123456, sum_price_kopecks=999999),
            make_stat(None, _ADVERT, _NM_SMALL, today - timedelta(days=1),
                      views=500, clicks=50, orders=5, shks=4,
                      sum_kopecks=1000, sum_price_kopecks=50000),
            Budget(advert_id=_ADVERT, cash_kopecks=5000, netting_kopecks=0,
                   total_kopecks=20000, captured_at=_T0 - timedelta(hours=1)),
            make_bid(None, _ADVERT, _NM_BIG, "search", 700, _T0 - timedelta(hours=2)),
            AuctionSnapshot(advert_id=_ADVERT, payload={"probe": "snapshot"},
                            parsed={"kind": "recommendations"},
                            captured_at=_T0 - timedelta(hours=3)),
        )
    )
    return cabinet_ids[-1]


# --- GET /cabinets/{id}/campaigns/{advert_id} --------------------------------


def test_campaign_page_renders_metrics_budget_and_charts(auth_client, cabinet_ids):
    cid = _seed_detail_cabinet(cabinet_ids)
    r = auth_client.get(f"/cabinets/{cid}/campaigns/{_ADVERT}?days=7")
    assert r.status_code == 200
    # header
    assert "РК Детально" in r.text
    assert "аукцион/ручная" in r.text  # type 8 decoded
    assert "активна" in r.text  # status 9 decoded
    assert str(_ADVERT) in r.text
    # money formatting (spend + revenue) and DRR
    assert "1234.56 BYN" in r.text
    assert "9999.99 BYN" in r.text
    assert "12.3%" in r.text  # drr 123456/999999
    # budget block (CPC campaign with a budget row): 50.00 / 200.00 BYN
    assert "Бюджет" in r.text
    assert "50.00 BYN" in r.text
    assert "200.00 BYN" in r.text
    # chart canvases + Chart.js + JSON data block
    assert r.text.count("<canvas") >= 3
    assert "chart.umd.js" in r.text
    assert 'id="chart-data"' in r.text
    assert '"bid_kopecks"' in r.text  # chart data carries the bid series
    # auction snapshots details block with formatted JSON
    assert "Снапшоты аукциона" in r.text
    assert "snapshot" in r.text
    # per-product table
    assert str(_NM_BIG) in r.text
    # days switcher
    assert "days=30" in r.text


def test_campaign_page_bad_days_is_422(auth_client, cabinet_ids):
    cid = _seed_detail_cabinet(cabinet_ids)
    r = auth_client.get(f"/cabinets/{cid}/campaigns/{_ADVERT}", params={"days": 13})
    assert r.status_code == 422


def test_campaign_page_unknown_campaign_is_404(auth_client, cabinet_ids):
    cid = _seed_detail_cabinet(cabinet_ids)
    r = auth_client.get(f"/cabinets/{cid}/campaigns/987654321?days=7")
    assert r.status_code == 404


def test_campaign_page_unknown_cabinet_is_404(auth_client):
    r = auth_client.get(f"/cabinets/999999/campaigns/{_ADVERT}?days=7")
    assert r.status_code == 404


def test_campaign_page_requires_login(client, cabinet_ids):
    cid = _seed_detail_cabinet(cabinet_ids)
    r = client.get(f"/cabinets/{cid}/campaigns/{_ADVERT}?days=7", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- GET /items ---------------------------------------------------------------


def test_items_page_rows_sorted_by_spent(auth_client, cabinet_ids):
    cid = _seed_detail_cabinet(cabinet_ids)
    r = auth_client.get("/items", params={"cabinet": cid})
    assert r.status_code == 200
    assert "ДРР" in r.text  # products table with a DRR column
    # biggest spender first
    assert r.text.index(str(_NM_BIG)) < r.text.index(str(_NM_SMALL))
    assert "1234.56 BYN" in r.text  # spent
    assert "9999.99 BYN" in r.text  # revenue


def test_items_page_without_cabinet_shows_chooser(auth_client, cabinet_ids):
    _seed_detail_cabinet(cabinet_ids)
    r = auth_client.get("/items")
    assert r.status_code == 200
    assert "T9 Page" in r.text  # the seeded cabinet is offered as a choice


def test_items_page_unknown_cabinet_is_404(auth_client):
    r = auth_client.get("/items", params={"cabinet": 424242})
    assert r.status_code == 404


def test_items_page_requires_login(client):
    r = client.get("/items", params={"cabinet": 1}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- GET /health-page ----------------------------------------------------------


def test_health_page_lists_loops_and_log(auth_client, cabinet_ids):
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T9 Health Cab"),
            make_collection_log(None, loop_name="campaigns", status="ok",
                                detail="rows=3", finished_at=_T0 - timedelta(minutes=2)),
            make_collection_log(None, loop_name="fullstats:777", status="error",
                                detail="429 too many requests",
                                finished_at=_T0 - timedelta(minutes=4)),
        )
    )
    r = auth_client.get("/health-page")
    assert r.status_code == 200
    assert "campaigns" in r.text
    assert "fullstats" in r.text  # grouped per-campaign rows show the base name
    assert "Свежесть" in r.text  # freshness column
    assert "Период" in r.text  # period column (from LOOPS_SPEC)
    assert "Журнал" in r.text
    assert "429 too many requests" in r.text  # log detail rendered


def test_health_page_requires_login(client):
    r = client.get("/health-page", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- e2e -------------------------------------------------------------------------


def test_e2e_navigation_no_5xx(auth_client, cabinet_ids):
    """login -> overview -> cabinet -> campaign -> items -> health, all 200."""
    cid = _seed_detail_cabinet(cabinet_ids)
    paths = [
        "/",
        f"/cabinets/{cid}",
        f"/cabinets/{cid}/campaigns/{_ADVERT}?days=7",
        f"/items?cabinet={cid}",
        "/health-page",
    ]
    for path in paths:
        r = auth_client.get(path, follow_redirects=True)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert r.status_code < 500
