"""Task 4 (phase 2) page tests: /hours — заказы по часам (тепловая карта).

Seeding follows tests/web/test_pages.py: the web routes persist through
app.db.SessionLocal (the bider_test database), so rows go in through a
throwaway engine before the request and the seeded cabinet is deleted
afterwards (FK ON DELETE CASCADE cleans the orders too).
"""

import asyncio
import json
import os
import re
from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Cabinet
from app.queries import msk_today
from tests.helpers import make_cabinet, make_order


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


_CHART_RE = re.compile(
    r'<script type="application/json" id="chart-data">(.*?)</script>', re.DOTALL
)


def _chart_rows(html: str) -> list[dict]:
    """Parse the 24-element chart payload out of the page's JSON block."""
    match = _CHART_RE.search(html)
    assert match is not None, "chart-data JSON block missing"
    return json.loads(match.group(1))


def _seed_hours_cabinet(cabinet_ids: list[int]) -> int:
    """Кабинет с заказами в разные часы/дни, включая отменённые и старые."""
    today = msk_today()
    cabinet_ids.append(
        _seed(
            make_cabinet(name="T4 Hours", wb_type="BY", currency="BYN"),
            make_order(None, "t4-a", today, 10, price_kopecks=5000),
            make_order(None, "t4-b", today, 10, price_kopecks=2500, nm_id=2),
            make_order(None, "t4-c", today - timedelta(days=3), 22, price_kopecks=777),
            # отменённые не считаются (ни заказы, ни выручка)
            make_order(None, "t4-cancelled", today, 3, price_kopecks=99999,
                       is_cancelled=True),
            # вне 30-дневного окна
            make_order(None, "t4-old", today - timedelta(days=40), 9, price_kopecks=1),
        )
    )
    return cabinet_ids[-1]


# --- GET /hours?cabinet= ------------------------------------------------------


def test_hours_page_renders_chart_and_table(auth_client, cabinet_ids):
    cid = _seed_hours_cabinet(cabinet_ids)
    r = auth_client.get("/hours", params={"cabinet": cid})
    assert r.status_code == 200
    assert "<h1>Часы</h1>" in r.text  # page header
    assert 'href="/hours"' in r.text  # nav link
    assert "T4 Hours" in r.text  # cabinet name in the header
    assert "BYN" in r.text  # cabinet currency
    assert "Заказов по часам" in r.text  # chart caption
    assert "<canvas" in r.text
    assert 'id="chart-data"' in r.text  # JSON payload for Chart.js
    assert "chart.umd.js" in r.text  # vendored Chart.js
    assert "аппроксимация" in r.text  # MSK-hour approximation note
    # таблица-раскладка: 24 строки, включая нулевые часы
    assert "00:00" in r.text
    assert "23:00" in r.text
    assert "Выручка" in r.text


def test_hours_page_chart_data_has_24_hours(auth_client, cabinet_ids):
    cid = _seed_hours_cabinet(cabinet_ids)
    r = auth_client.get("/hours", params={"cabinet": cid})
    rows = _chart_rows(r.text)
    assert [row["hour"] for row in rows] == list(range(24))
    assert rows[10] == {"hour": 10, "orders": 2, "revenue": 7500}
    assert rows[22] == {"hour": 22, "orders": 1, "revenue": 777}
    assert rows[3]["orders"] == 0  # отменённый заказ не считается
    assert rows[3]["revenue"] == 0
    assert rows[9]["orders"] == 0  # вне 30-дневного окна


def test_hours_page_money_formatted_in_cabinet_currency(auth_client, cabinet_ids):
    """Выручка в таблице идёт через format_money в валюте кабинета."""
    cid = _seed_hours_cabinet(cabinet_ids)
    r = auth_client.get("/hours", params={"cabinet": cid})
    assert "75.00 BYN" in r.text  # hour 10: 5000 + 2500 kopecks
    assert "7.77 BYN" in r.text  # hour 22
    assert "0.00 BYN" in r.text  # zero hours are in the table too


def test_hours_page_period_switch_7_30(auth_client, cabinet_ids):
    today = msk_today()
    cid = _seed(
        make_cabinet(name="T4 Days"),
        make_order(None, "t4-d8", today - timedelta(days=8), 14, price_kopecks=10000),
    )
    cabinet_ids.append(cid)
    r7 = auth_client.get("/hours", params={"cabinet": cid, "days": 7})
    assert r7.status_code == 200
    assert "?days=7" in r7.text and "?days=30" in r7.text  # переключатель периода
    assert 'class="filter-on"' in r7.text  # активный период подсвечен
    assert _chart_rows(r7.text)[14]["orders"] == 0  # 8 дней назад не в 7-дневном окне

    r30 = auth_client.get("/hours", params={"cabinet": cid, "days": 30})
    assert r30.status_code == 200
    assert _chart_rows(r30.text)[14] == {"hour": 14, "orders": 1, "revenue": 10000}


def test_hours_page_bad_days_is_422(auth_client, cabinet_ids):
    cid = _seed_hours_cabinet(cabinet_ids)
    r = auth_client.get("/hours", params={"cabinet": cid, "days": 13})
    assert r.status_code == 422
    r_text = auth_client.get("/hours", params={"cabinet": cid, "days": "abc"})
    assert r_text.status_code == 422  # FastAPI validation


# --- /hours без параметров, 404, auth ------------------------------------------


def test_hours_page_without_cabinet_shows_chooser(auth_client, cabinet_ids):
    _seed_hours_cabinet(cabinet_ids)
    r = auth_client.get("/hours")
    assert r.status_code == 200
    assert "T4 Hours" in r.text  # кабинет предложен как вариант выбора
    assert "/hours?cabinet=" in r.text
    assert 'id="chart-data"' not in r.text  # без выбранного кабинета графика нет


def test_hours_page_unknown_cabinet_is_404(auth_client):
    r = auth_client.get("/hours", params={"cabinet": 424242})
    assert r.status_code == 404


def test_hours_page_requires_login(client, cabinet_ids):
    cid = _seed_hours_cabinet(cabinet_ids)
    r = client.get(f"/hours?cabinet={cid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
