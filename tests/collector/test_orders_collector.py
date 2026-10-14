"""Orders collector tests: pure parsing + fake statistics client + real Postgres.

Row formats follow .scratch/facts-phase2.md («ЖИВЫЕ ФАКТЫ прод-прогона»,
14.10.2026): lastChangeDate "2026-10-12T21:21:39" БЕЗ таймзоны (МСК),
totalPrice/finishedPrice — целые ЕДИНИЦЫ валюты (→ копейки ×100), isCancel
bool, cancelDate "0001-01-01T00:00:00" у неотменённых, srid — строка (PK).

The fake client implements the flag=0 pagination contract: each call records
its dateFrom and pops the next canned page; an empty page ends the fetch.
Sessions come from the db_session fixture (per-test savepoint).
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.orders import collect_orders, parse_orders
from app.crypto import encrypt_token
from app.models import Cabinet, CollectionLog, Order
from app.repos import upsert_orders
from app.wb.errors import WbApiError

_MSK = timezone(timedelta(hours=3))
# DB-backed tests must share the session-scoped loop of the db_session fixture.
_db = pytest.mark.asyncio(loop_scope="session")


def _msk(*args: int) -> datetime:
    return datetime(*args, tzinfo=_MSK)


class FakeStatsClient:
    """StatisticsClient stand-in: records dateFrom calls, pops canned pages."""

    def __init__(self, *pages: list[dict]) -> None:
        self.pages = list(pages)
        self.calls: list[datetime] = []

    async def orders(self, date_from: datetime) -> list[dict]:
        self.calls.append(date_from)
        return self.pages.pop(0) if self.pages else []


class BoomClient:
    """StatisticsClient stand-in whose orders() always fails."""

    def __init__(self, exc: WbApiError) -> None:
        self.exc = exc

    async def orders(self, date_from: datetime) -> list[dict]:
        raise self.exc


def _live_row(
    srid: str,
    last_change: str,
    *,
    order_date: str = "2026-10-12",
    finished_price: float = 355,
    nm_id: int = 111,
    is_cancel: bool = False,
    cancel_date: str = "0001-01-01T00:00:00",
) -> dict:
    """One supplier/orders row exactly as the live prod response shapes it."""
    return {
        "date": order_date,
        "lastChangeDate": last_change,
        "warehouseName": "Коледино",
        "nmId": nm_id,
        "totalPrice": 1010,
        "finishedPrice": finished_price,
        "isCancel": is_cancel,
        "cancelDate": cancel_date,
        "gNumber": "g90001",
        "srid": srid,
    }


# --- parse_orders (pure) ------------------------------------------------------


def test_parse_orders_live_row_shape():
    (row,) = parse_orders(
        [_live_row("ei.iabc.0.0", "2026-10-12T21:21:39", order_date="2026-10-12")],
        cabinet_id=7,
    )
    assert row == {
        "srid": "ei.iabc.0.0",
        "cabinet_id": 7,
        "nm_id": 111,
        "date": date(2026, 10, 12),
        # час заказа = час lastChangeDate (у `date` времени нет) — аппроксимация
        "hour": 21,
        "price_kopecks": 35500,  # единицы валюты -> копейки
        "date_time": _msk(2026, 10, 12, 21, 21, 39),  # МСК, aware
        "is_cancelled": False,
    }


def test_parse_orders_cancelled_via_iscancel_and_via_canceldate():
    rows = parse_orders(
        [
            _live_row("s1", "2026-10-12T10:00:00", is_cancel=True,
                      cancel_date="2026-10-13T08:00:00"),
            # isCancel=false, но cancelDate не заглушка — трактуем как отмену
            _live_row("s2", "2026-10-12T10:00:00", is_cancel=False,
                      cancel_date="2026-10-13T08:00:00"),
            _live_row("s3", "2026-10-12T10:00:00", is_cancel=False),
        ],
        cabinet_id=1,
    )
    assert [row["is_cancelled"] for row in rows] == [True, True, False]


def test_parse_orders_float_price_and_garbage_rows():
    payload: list = [
        _live_row("f1", "2026-10-12T09:05:00", finished_price=355.4),
        "garbage",  # не-словарные элементы игнорируются
        42,
        {"no_srid": True},  # нет ключа upsert — строка пропускается
    ]
    rows = parse_orders(payload, cabinet_id=1)
    assert [row["price_kopecks"] for row in rows] == [35540]


def test_parse_orders_none_and_empty_payload():
    assert parse_orders(None, cabinet_id=1) == []
    assert parse_orders([], cabinet_id=1) == []


# --- upsert_orders ------------------------------------------------------------


@_db
async def test_upsert_orders_insert_then_conflict_updates(db_session: AsyncSession):
    cab = await _cabinet_id(db_session)
    rows = [dict(parse_orders([_live_row("srid-x", "2026-10-12T10:00:00")], cab)[0])]

    assert await upsert_orders(db_session, cab, rows) == 1
    # тот же srid: метрики (цена/отмена/время) обновляются, строка не плодится
    rows[0]["price_kopecks"] = 99900
    rows[0]["is_cancelled"] = True
    assert await upsert_orders(db_session, cab, rows) == 1

    orders = (await db_session.execute(select(Order))).scalars().all()
    assert len(orders) == 1
    assert (orders[0].price_kopecks, orders[0].is_cancelled) == (99900, True)


@_db
async def test_upsert_orders_folds_duplicate_srid_last_wins(db_session: AsyncSession):
    """Дубль srid ВНУТРИ одной страницы не роняет upsert; последний winning."""
    cab = await _cabinet_id(db_session)
    parsed = parse_orders(
        [
            _live_row("dup", "2026-10-12T10:00:00", finished_price=100),
            _live_row("dup", "2026-10-12T10:00:00", finished_price=200,
                      is_cancel=True, cancel_date="2026-10-13T08:00:00"),
        ],
        cab,
    )
    assert await upsert_orders(db_session, cab, parsed) == 1

    (order,) = (await db_session.execute(select(Order))).scalars().all()
    assert (order.price_kopecks, order.is_cancelled) == (20000, True)


# --- collect_orders (integration) -----------------------------------------------


@_db
async def test_collect_orders_retro_dedup_cancel_and_log(db_session: AsyncSession):
    cab = await _cabinet_id(db_session)
    page = [
        _live_row("srid-a", "2026-10-10T10:00:00", order_date="2026-10-10",
                  finished_price=100),
        _live_row("srid-b", "2026-10-12T21:21:39", order_date="2026-10-12",
                  finished_price=355.4, is_cancel=True,
                  cancel_date="2026-10-13T08:00:00"),
        _live_row("srid-a", "2026-10-10T10:00:00", order_date="2026-10-10",
                  finished_price=100),  # дубль srid в той же выгрузке
    ]
    client = FakeStatsClient(page)

    written = await collect_orders(client, db_session, cab)

    # дубль srid схлопывается до upsert'а (PostgreSQL не даёт ON CONFLICT
    # затронуть строку дважды), поэтому затронуто 2 уникальных строки
    assert written == 2
    orders = (await db_session.execute(select(Order).order_by(Order.srid))).scalars().all()
    assert [o.srid for o in orders] == ["srid-a", "srid-b"]  # дубль схлопнут
    a, b = orders
    assert (a.nm_id, a.hour, a.price_kopecks, a.is_cancelled) == (111, 10, 10000, False)
    assert a.date_time == _msk(2026, 10, 10, 10, 0, 0)
    assert (b.price_kopecks, b.is_cancelled) == (35540, True)

    # заказов в БД не было: первый запрос — полный ретро-прогон от now(МСК) - 90 дней
    assert len(client.calls) == 2  # страница + пустой ответ (всё выгружено)
    expected_start = datetime.now(_MSK) - timedelta(days=90)
    assert abs((client.calls[0] - expected_start).total_seconds()) < 120
    # пагинация: следующий запрос от последнего lastChangeDate страницы
    assert client.calls[1] == _msk(2026, 10, 12, 21, 21, 39)

    log = (
        await db_session.execute(
            select(CollectionLog).where(
                CollectionLog.cabinet_id == cab, CollectionLog.loop_name == "orders"
            )
        )
    ).scalar_one()
    assert log.status == "ok"
    assert log.detail == "processed=2"


@_db
async def test_collect_orders_second_run_is_idempotent_and_uses_cursor(db_session: AsyncSession):
    cab = await _cabinet_id(db_session)
    page = [
        _live_row("srid-a", "2026-10-10T10:00:00", order_date="2026-10-10"),
        _live_row("srid-b", "2026-10-12T21:21:39", order_date="2026-10-12"),
    ]
    await collect_orders(FakeStatsClient(page), db_session, cab)

    # второй прогон: dateFrom = последний успешный lastChangeDate - 1 час перекрытия
    client = FakeStatsClient(page, [])
    await collect_orders(client, db_session, cab)

    assert client.calls[0] == _msk(2026, 10, 12, 20, 21, 39)
    total = (
        await db_session.execute(select(func.count()).select_from(Order))
    ).scalar_one()
    assert total == 2  # upsert идемпотентен: число строк не растёт


@_db
async def test_collect_orders_paginates_until_empty_page(db_session: AsyncSession):
    cab = await _cabinet_id(db_session)
    client = FakeStatsClient(
        [_live_row("p1", "2026-10-12T10:00:00")],
        [_live_row("p2", "2026-10-12T11:30:05")],
    )
    written = await collect_orders(client, db_session, cab)

    assert written == 2
    assert client.calls[1] == _msk(2026, 10, 12, 10, 0, 0)  # от последнего lc 1-й страницы
    assert client.calls[2] == _msk(2026, 10, 12, 11, 30, 5)  # от последнего lc 2-й
    assert len(client.calls) == 3  # третий запрос вернул [] — выгрузка закончена
    total = (
        await db_session.execute(select(func.count()).select_from(Order))
    ).scalar_one()
    assert total == 2


@_db
async def test_collect_orders_stuck_page_advances_cursor_by_second(db_session: AsyncSession):
    """Страница, чей максимум lastChangeDate не ушёл за dateFrom, не зацикливается."""
    cab = await _cabinet_id(db_session)
    await upsert_orders(
        db_session,
        cab,
        parse_orders([_live_row("seed", "2026-10-12T12:00:00")], cab),
    )

    # dateFrom = 12:00 - 1ч = 11:00; страница целиком с lc == 11:00
    client = FakeStatsClient([_live_row("same", "2026-10-12T11:00:00")])
    await collect_orders(client, db_session, cab)

    assert client.calls[0] == _msk(2026, 10, 12, 11, 0, 0)
    # курсор подвинулся на секунду (разрешение lastChangeDate — секунды)
    assert client.calls[1] == _msk(2026, 10, 12, 11, 0, 1)


@_db
async def test_collect_orders_wb_error_logged_then_raised(db_session: AsyncSession):
    cab = await _cabinet_id(db_session)
    client = BoomClient(WbApiError(403, "scope is not allowed for this resource"))

    with pytest.raises(WbApiError):
        await collect_orders(client, db_session, cab)

    log = (
        await db_session.execute(
            select(CollectionLog).where(
                CollectionLog.cabinet_id == cab, CollectionLog.loop_name == "orders"
            )
        )
    ).scalar_one()
    assert log.status == "error"
    assert "scope" in log.detail


# --- helpers -----------------------------------------------------------------


async def _cabinet_id(db_session: AsyncSession) -> int:
    cabinet = Cabinet(
        name="orders-test", wb_type="BY", currency="BYN",
        token_encrypted=encrypt_token("tok"),
    )
    db_session.add(cabinet)
    await db_session.commit()
    return cabinet.id
