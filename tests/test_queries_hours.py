"""hours_heatmap query tests (app/queries.py) on real Postgres.

Orders are seeded straight into the orders table (phase-2 schema); the
heatmap aggregates by the stored MSK hour, excludes cancelled orders and
pads the result to all 24 hours. Window = last N days inclusive of today by
the Moscow calendar (same semantics as overview's 7d/30d buckets).
Each test runs inside the db_session savepoint and is rolled back.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Cabinet, Order
from app.queries import hours_heatmap, msk_today

# The engine/db_session fixtures live on the session-scoped event loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")

_MSK = timezone(timedelta(hours=3))


def _order(cabinet_id: int, srid: str, day, hour: int, *, price_kopecks: int = 1000,
           cancelled: bool = False, nm_id: int = 1) -> Order:
    return Order(
        srid=srid,
        cabinet_id=cabinet_id,
        nm_id=nm_id,
        date=day,
        hour=hour,
        price_kopecks=price_kopecks,
        date_time=datetime(day.year, day.month, day.day, hour, tzinfo=_MSK),
        is_cancelled=cancelled,
    )


async def test_hours_heatmap_counts_revenue_excludes_cancelled_pads_24(
    db_session: AsyncSession,
):
    today = msk_today()
    cab = Cabinet(name="Heat", wb_type="BY", currency="BYN",
                  token_encrypted="x")
    other = Cabinet(name="Other", wb_type="BY", currency="BYN",
                    token_encrypted="x")
    db_session.add_all([cab, other])
    await db_session.flush()

    db_session.add_all(
        [
            _order(cab.id, "1", today, 10, price_kopecks=5000),
            _order(cab.id, "2", today, 10, price_kopecks=2500, nm_id=2),
            _order(cab.id, "3", today - timedelta(days=3), 22, price_kopecks=777),
            # отменённые не считаются (ни заказы, ни выручка)
            _order(cab.id, "4", today, 3, price_kopecks=99999, cancelled=True),
            # вне окна 30 дней
            _order(cab.id, "5", today - timedelta(days=40), 9),
            # чужой кабинет не протекает
            _order(other.id, "6", today, 15),
        ]
    )
    await db_session.commit()

    rows = await hours_heatmap(db_session, cab.id, 30)

    assert [row["hour"] for row in rows] == list(range(24))  # полные 24 часа
    assert rows[10] == {"hour": 10, "orders": 2, "revenue_kopecks": 7500}
    assert rows[22] == {"hour": 22, "orders": 1, "revenue_kopecks": 777}
    assert rows[3] == {"hour": 3, "orders": 0, "revenue_kopecks": 0}  # отменённый
    assert rows[9] == {"hour": 9, "orders": 0, "revenue_kopecks": 0}  # вне окна
    assert rows[15] == {"hour": 15, "orders": 0, "revenue_kopecks": 0}  # чужой кабинет
    assert sum(row["orders"] for row in rows) == 3


async def test_hours_heatmap_window_filters_by_days(db_session: AsyncSession):
    today = msk_today()
    cab = Cabinet(name="Heat7", wb_type="BY", currency="BYN",
                  token_encrypted="x")
    db_session.add(cab)
    await db_session.flush()
    db_session.add_all(
        [
            _order(cab.id, "in-7", today - timedelta(days=5), 8),
            _order(cab.id, "out-7", today - timedelta(days=8), 9),
        ]
    )
    await db_session.commit()

    rows_7 = await hours_heatmap(db_session, cab.id, 7)
    assert rows_7[8]["orders"] == 1
    assert rows_7[9]["orders"] == 0

    rows_30 = await hours_heatmap(db_session, cab.id, 30)
    assert rows_30[8]["orders"] == 1
    assert rows_30[9]["orders"] == 1  # попадает в 30 дней


async def test_hours_heatmap_empty_cabinet_all_zeroes(db_session: AsyncSession):
    cab = Cabinet(name="Empty", wb_type="BY", currency="BYN",
                  token_encrypted="x")
    db_session.add(cab)
    await db_session.commit()

    rows = await hours_heatmap(db_session, cab.id, 7)

    assert rows == [
        {"hour": hour, "orders": 0, "revenue_kopecks": 0} for hour in range(24)
    ]
