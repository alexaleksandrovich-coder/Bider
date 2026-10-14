"""Orders collector: statistics-api supplier/orders -> app.models.Order.

Row format (живые факты, спайк-2 прод 14.10.2026, .scratch/facts-phase2.md):
lastChangeDate "2026-10-12T21:21:39" — БЕЗ таймзоны, МСК; деньги
(totalPrice/finishedPrice) — ЦЕЛЫЕ ЕДИНИЦЫ валюты → в БД копейки (×100);
отмена = isCancel (cancelDate "0001-01-01T00:00:00" — заглушка живого
заказа); srid — строковый PK; `date` — только дата заказа.

Increment strategy (flag=0): rows with lastChangeDate >= dateFrom come back,
up to ~80000 per response; the next request resumes from the newest
lastChangeDate of the previous page; an empty response means everything is
fetched. WB keeps 90 days of orders and refreshes them every 30 minutes, so
the first run does a one-off retro pass from now-90d (pagination until the
empty page), later runs use a 1-hour-overlap cursor from the newest stored
date_time.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order
from app.repos import log_collection, upsert_orders
from app.wb.errors import WbApiError
from app.wb.parsers import _money_to_kopecks, _to_date, _to_int
from app.wb.statistics_client import ORDERS_PATH, StatisticsClient

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))  # statistics-api: Moscow, UTC+3, no DST
_CANCEL_SENTINEL = "0001-01-01T00:00:00"  # cancelDate placeholder of a live order
_HISTORY_DAYS = 90  # WB keeps orders for 90 days — the one-off retro window
_OVERLAP = timedelta(hours=1)  # cursor rewinds 1h: late lastChangeDate updates
_MAX_PAGES_BACKFILL = 50  # safety caps for the pagination loop
_MAX_PAGES_INCREMENTAL = 10


def _parse_msk(value: Any) -> datetime | None:
    """ISO datetime string -> aware MSK datetime; naive input IS MSK.

    Живой факт: lastChangeDate приходит без таймзоны — это МСК (UTC+3).
    """
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_MSK)
    return moment.astimezone(_MSK)


def parse_orders(payload: list[dict] | None, cabinet_id: int) -> list[dict]:
    """Pure parser: supplier/orders rows -> upsert rows for repos.upsert_orders.

    Non-dict elements and rows without srid/lastChangeDate are skipped (there
    is nothing to key the upsert on). The hour comes from lastChangeDate: the
    live `date` field carries no time (ЖИВОЙ ФАКТ), so the order hour is an
    approximation by the minute of the last change — sufficient for the
    hourly heatmap.
    """
    if not payload:
        return []
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        srid = item.get("srid")
        last_change = _parse_msk(item.get("lastChangeDate"))
        if not srid or last_change is None:
            continue
        cancel_date = str(item.get("cancelDate") or "")
        rows.append(
            {
                "srid": str(srid),
                "cabinet_id": cabinet_id,
                "nm_id": _to_int(item.get("nmId")),
                "date": _to_date(item.get("date")) or last_change.date(),
                "hour": last_change.hour,
                "price_kopecks": _money_to_kopecks(item.get("finishedPrice")),
                "date_time": last_change,
                "is_cancelled": bool(item.get("isCancel"))
                or (cancel_date != "" and cancel_date != _CANCEL_SENTINEL),
            }
        )
    return rows


async def _last_order_moment(session: AsyncSession, cabinet_id: int) -> datetime | None:
    """Newest stored date_time (lastChangeDate) of the cabinet's orders."""
    return (
        await session.execute(
            select(func.max(Order.date_time)).where(Order.cabinet_id == cabinet_id)
        )
    ).scalar_one_or_none()


async def collect_orders(
    client: StatisticsClient, session: AsyncSession, cabinet_id: int
) -> int:
    """Incremental flag=0 fetch with pagination until an empty page.

    Cursor: the newest stored date_time minus 1 hour of overlap; with no
    orders yet — now(MSK) - 90 days (one-off retro pass, WB's full history
    window). Each next request resumes from the newest lastChangeDate of the
    previous page; a page that does not move the cursor forward (rows exactly
    at dateFrom — lastChangeDate has second resolution) advances it by 1s so
    the loop always terminates. Returns the number of rows touched by the
    upserts (inserted + refreshed). WbApiError is logged "error" and
    re-raised — IntervalLoop counts consecutive failures itself.
    """
    from app.collector.runner import save_raw  # deferred: runner imports this module

    cursor = await _last_order_moment(session, cabinet_id)
    if cursor is None:
        date_from = datetime.now(_MSK) - timedelta(days=_HISTORY_DAYS)
        max_pages = _MAX_PAGES_BACKFILL
    else:
        date_from = cursor - _OVERLAP
        max_pages = _MAX_PAGES_INCREMENTAL

    written = 0
    try:
        for _ in range(max_pages):
            payload = await client.orders(date_from)
            await save_raw(session, cabinet_id, ORDERS_PATH, 200, payload)
            rows = parse_orders(payload, cabinet_id)
            if not rows:
                break  # empty page: everything up to now is fetched
            written += await upsert_orders(session, cabinet_id, rows)
            newest = max(row["date_time"] for row in rows)
            date_from = (
                newest if newest > date_from else date_from + timedelta(seconds=1)
            )
    except WbApiError as exc:
        logger.error("orders (cabinet %s) failed: %s", cabinet_id, exc)
        await log_collection(
            session, cabinet_id, "orders", "error", str(exc), None, _now_utc()
        )
        raise
    await log_collection(
        session, cabinet_id, "orders", "ok", f"processed={written}", None, _now_utc()
    )
    return written


def _now_utc() -> datetime:
    return datetime.now(UTC)
