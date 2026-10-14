"""Dashboard page tests (Task 8): overview, cabinet campaigns, admin cabinets.

The web routes persist through app.db.SessionLocal (the bider_test database),
so seeding happens through a throwaway engine before the request, and every
seeded cabinet is deleted afterwards (FK ON DELETE CASCADE cleans children).
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crypto import decrypt_token
from app.models import Cabinet
from app.queries import msk_today
from tests.helpers import (
    DEFAULT_TOKEN,
    make_balance,
    make_bid,
    make_cabinet,
    make_campaign,
    make_collection_log,
    make_stat,
)

RAW_TOKEN = "x-page-test-raw-token"
_T0 = datetime.now(UTC)


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


def _seed(cabinet: Cabinet, *children) -> int:
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


# --- GET / (overview) --------------------------------------------------------


def test_overview_shows_cabinet_sums_balance_freshness(auth_client, cabinet_ids):
    # now вычисляется В тесте, а не при импорте модуля: ассерты rel_time
    # («2 мин назад») имеют узкое окно и ломались на долгих прогонах.
    now = datetime.now(UTC)
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T8 Alpha BY"),
            make_stat(None, 101, 1, msk_today(), sum_kopecks=123456),
            make_balance(None, 500000, now - timedelta(minutes=5)),
            make_collection_log(None, finished_at=now - timedelta(minutes=2)),
        )
    )
    r = auth_client.get("/")
    assert r.status_code == 200
    assert "T8 Alpha BY" in r.text
    assert "1234.56 BYN" in r.text  # spent today
    assert "5000.00 BYN" in r.text  # balance
    assert "2 мин назад" in r.text  # freshness


def test_overview_marks_stale_when_collection_is_old(auth_client, cabinet_ids):
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T8 Stale"),
            make_collection_log(None, finished_at=_T0 - timedelta(hours=2)),
        )
    )
    r = auth_client.get("/")
    assert r.status_code == 200
    assert "2 ч назад" in r.text
    assert "stale" in r.text


def test_overview_fresh_cabinet_has_no_stale_marker(auth_client, cabinet_ids):
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T8 Fresh"),
            make_collection_log(None, finished_at=_T0 - timedelta(minutes=2)),
        )
    )
    r = auth_client.get("/")
    assert r.status_code == 200
    assert "stale" not in r.text


def test_overview_never_collected_cabinet_explains_when_to_expect_data(
    auth_client, cabinet_ids
):
    """A cabinet without any collection_log row points to the manual sync."""
    cabinet_ids.append(_seed(make_cabinet(name="T8 Never")))
    r = auth_client.get("/")
    assert r.status_code == 200
    assert "данные появятся после первого сбора" in r.text
    assert "Обновить сейчас" in r.text
    assert 'href="/admin/cabinets"' in r.text
    # a cabinet WITH log rows keeps the ordinary relative-time freshness
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T8 Collected"),
            make_collection_log(None, finished_at=_T0 - timedelta(minutes=2)),
        )
    )
    r = auth_client.get("/")
    assert r.text.count("данные появятся после первого сбора") == 1
    assert "T8 Collected" in r.text


# --- GET /cabinets/{id} ------------------------------------------------------


def _seed_campaigns_cabinet(cabinet_ids: list[int]) -> int:
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T8 Cab Page"),
            make_campaign(None, 3001, name="Поисковая РК", type=1, status=9,
                          payment_type="cpc", bid_type="auto",
                          placement_search=True, placement_reco=False),
            make_campaign(None, 3002, name="Паузная РК", type=8, status=11,
                          payment_type="cpm", bid_type="manual",
                          placement_search=False, placement_reco=True),
            make_bid(None, 3001, 11, "search", 500, _T0 - timedelta(hours=1)),
            make_bid(None, 3001, 11, "search", 700, _T0),
            make_bid(None, 3002, 12, "recommendations", 300, _T0 - timedelta(hours=1)),
        )
    )
    return cabinet_ids[-1]


def test_cabinet_page_lists_campaigns_with_latest_bids(auth_client, cabinet_ids):
    cid = _seed_campaigns_cabinet(cabinet_ids)
    r = auth_client.get(f"/cabinets/{cid}")
    assert r.status_code == 200
    assert "T8 Cab Page" in r.text
    assert "Поисковая РК" in r.text
    assert "Паузная РК" in r.text
    assert "SEO/поиск" in r.text  # type 1 decoded
    assert "аукцион/ручная" in r.text  # type 8 decoded
    assert "активна" in r.text  # status 9 badge
    assert "пауза" in r.text  # status 11 badge
    assert "7.00 BYN" in r.text  # latest search bid (700), not the older 500
    assert "3.00 BYN" in r.text  # recommendations bid


def test_cabinet_page_status_filter(auth_client, cabinet_ids):
    cid = _seed_campaigns_cabinet(cabinet_ids)
    active = auth_client.get(f"/cabinets/{cid}", params={"status": "active"})
    assert active.status_code == 200
    assert "Поисковая РК" in active.text
    assert "Паузная РК" not in active.text

    paused = auth_client.get(f"/cabinets/{cid}", params={"status": "paused"})
    assert paused.status_code == 200
    assert "Паузная РК" in paused.text
    assert "Поисковая РК" not in paused.text


def test_cabinet_page_unknown_id_is_404(auth_client):
    r = auth_client.get("/cabinets/999999")
    assert r.status_code == 404


def test_cabinet_page_requires_login(client, cabinet_ids):
    cid = _seed_campaigns_cabinet(cabinet_ids)
    r = client.get(f"/cabinets/{cid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- GET/POST /admin/cabinets -------------------------------------------------


def test_admin_cabinets_page_shows_form_and_list(auth_client, cabinet_ids):
    cabinet_ids.append(_seed(make_cabinet(name="T8 Listed", token=RAW_TOKEN)))
    r = auth_client.get("/admin/cabinets")
    assert r.status_code == 200
    assert 'action="/admin/cabinets"' in r.text
    assert 'name="name"' in r.text
    assert 'name="wb_type"' in r.text
    assert 'value="BY"' in r.text
    assert 'value="RU"' in r.text
    assert 'name="token"' in r.text
    assert "T8 Listed" in r.text
    # the token must never be rendered, not even encrypted
    assert RAW_TOKEN not in r.text
    assert DEFAULT_TOKEN not in r.text


def test_admin_cabinets_post_creates_encrypted_cabinet(auth_client, cabinet_ids):
    r = auth_client.post(
        "/admin/cabinets",
        data={"name": "T8 New RU", "wb_type": "RU", "token": RAW_TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/cabinets?added=")

    async def _load(db):
        return (
            (await db.execute(select(Cabinet).where(Cabinet.name == "T8 New RU")))
            .scalars()
            .one()
        )

    cab = _run_db(_load)
    cabinet_ids.append(cab.id)
    assert cab.wb_type == "RU"
    assert cab.currency == "RUB"
    assert cab.token_encrypted != RAW_TOKEN  # never stored as plaintext
    assert decrypt_token(cab.token_encrypted) == RAW_TOKEN

    page = auth_client.get("/admin/cabinets")
    assert "T8 New RU" in page.text
    assert RAW_TOKEN not in page.text  # token never rendered


def test_admin_cabinets_post_invalid_wb_type_is_form_error(auth_client, cabinet_ids):
    r = auth_client.post(
        "/admin/cabinets",
        data={"name": "T8 Bad", "wb_type": "XX", "token": RAW_TOKEN},
    )
    assert r.status_code == 422  # form error, not a 500

    async def _count(db):
        return await db.scalar(select(func.count()).select_from(Cabinet).where(Cabinet.name == "T8 Bad"))

    assert _run_db(_count) == 0  # nothing saved
