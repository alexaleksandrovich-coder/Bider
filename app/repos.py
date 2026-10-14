"""Repository functions: parsed rows -> PostgreSQL, one transaction per call.

Every function executes its statements and COMMITS before returning (tests
and the collector call these directly), then reports the number of affected
rows (upserts count inserted+updated rows). Money columns (*_kopecks) store
values exactly as parsed — int kopecks, scale pending spike confirmation
(see app/wb/parsers.py header and .scratch/facts.md).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuctionSnapshot,
    Balance,
    BidHistory,
    Budget,
    Campaign,
    Cluster,
    CollectionLog,
    Order,
    Position,
    Stat,
)

# Metric columns refreshed on every stats upsert (key columns are the conflict target).
_STAT_METRICS = (
    "views",
    "clicks",
    "atbs",
    "orders",
    "shks",
    "canceled",
    "sum_kopecks",
    "sum_price_kopecks",
    "cpc_kopecks",
)


async def upsert_campaigns(session: AsyncSession, cabinet_id: int, rows: list[dict]) -> int:
    """Insert or update campaigns by (cabinet_id, advert_id); updated_at = now()."""
    if not rows:
        return 0
    values = [
        {
            "cabinet_id": cabinet_id,
            "advert_id": row["advert_id"],
            "name": row["name"],
            "type": row["type"],
            "status": row["status"],
            "payment_type": row["payment_type"],
            "bid_type": row["bid_type"],
            "placement_search": bool(row.get("search", False)),
            "placement_reco": bool(row.get("recommendations", False)),
            "change_time": row.get("change_time"),
            "updated_at": func.now(),
        }
        for row in rows
    ]
    stmt = pg_insert(Campaign).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["cabinet_id", "advert_id"],
        set_={
            "name": stmt.excluded.name,
            "type": stmt.excluded.type,
            "status": stmt.excluded.status,
            "payment_type": stmt.excluded.payment_type,
            "bid_type": stmt.excluded.bid_type,
            "placement_search": stmt.excluded.placement_search,
            "placement_reco": stmt.excluded.placement_reco,
            "change_time": stmt.excluded.change_time,
            "updated_at": func.now(),
        },
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount)


async def append_bids(
    session: AsyncSession, cabinet_id: int, rows: list[dict], captured_at: datetime
) -> int:
    """Append each campaign's bids (from parse_adverts) to bid_history."""
    values = [
        {
            "cabinet_id": cabinet_id,
            "advert_id": row["advert_id"],
            "nm_id": bid["nm_id"],
            "placement": bid["placement"],
            "bid_kopecks": bid["bid_kopecks"],
            "captured_at": captured_at,
        }
        for row in rows
        for bid in row.get("bids", [])
    ]
    if not values:
        return 0
    result = await session.execute(insert(BidHistory).values(values))
    await session.commit()
    return int(result.rowcount)


async def upsert_stats(session: AsyncSession, cabinet_id: int, stat_rows: list[dict]) -> int:
    """Insert or update stats by the full PK (cabinet, advert, nm, date, app_type).

    Idempotent: writing the same rows twice leaves values unchanged.
    """
    if not stat_rows:
        return 0
    values = [{"cabinet_id": cabinet_id, **row} for row in stat_rows]
    stmt = pg_insert(Stat).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["cabinet_id", "advert_id", "nm_id", "date", "app_type"],
        set_={column: getattr(stmt.excluded, column) for column in _STAT_METRICS},
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount)


async def upsert_positions(
    session: AsyncSession, cabinet_id: int, position_rows: list[dict]
) -> int:
    """Insert or update positions by (cabinet_id, advert_id, nm_id, date)."""
    if not position_rows:
        return 0
    values = [
        {
            "cabinet_id": cabinet_id,
            "advert_id": row["advert_id"],
            "nm_id": row["nm_id"],
            "date": row["date"],
            # Decimal(str(x)) keeps the decimal literal exact for numeric(10,2)
            "avg_position": Decimal(str(row["avg_position"])),
        }
        for row in position_rows
    ]
    stmt = pg_insert(Position).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["cabinet_id", "advert_id", "nm_id", "date"],
        set_={"avg_position": stmt.excluded.avg_position},
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount)


async def insert_budgets(
    session: AsyncSession, cabinet_id: int, budget_rows: list[dict], captured_at: datetime
) -> int:
    """Append budget observations (from parse_budget) to budgets."""
    values = [
        {
            "cabinet_id": cabinet_id,
            "advert_id": row["advert_id"],
            "cash_kopecks": row["cash"],
            "netting_kopecks": row["netting"],
            "total_kopecks": row["total"],
            "captured_at": captured_at,
        }
        for row in budget_rows
    ]
    if not values:
        return 0
    result = await session.execute(insert(Budget).values(values))
    await session.commit()
    return int(result.rowcount)


async def insert_balance(
    session: AsyncSession, cabinet_id: int, total_kopecks: int, captured_at: datetime
) -> int:
    """Append one cabinet balance observation."""
    result = await session.execute(
        insert(Balance).values(
            cabinet_id=cabinet_id, total_kopecks=total_kopecks, captured_at=captured_at
        )
    )
    await session.commit()
    return int(result.rowcount)


async def insert_snapshot(
    session: AsyncSession,
    cabinet_id: int,
    advert_id: int,
    payload: dict,
    parsed: dict | None,
    captured_at: datetime,
) -> int:
    """Store one auction snapshot (raw payload + parsed view)."""
    result = await session.execute(
        insert(AuctionSnapshot).values(
            cabinet_id=cabinet_id,
            advert_id=advert_id,
            payload=payload,
            parsed=parsed,
            captured_at=captured_at,
        )
    )
    await session.commit()
    return int(result.rowcount)


async def upsert_clusters(
    session: AsyncSession, cabinet_id: int, advert_id: int, clusters: list[dict]
) -> int:
    """Insert or update clusters by (cabinet_id, advert_id, norm_query).

    Cluster rows: {norm_query, plus_type?, stats?, bid_kopecks?}.
    """
    if not clusters:
        return 0
    values = [
        {
            "cabinet_id": cabinet_id,
            "advert_id": advert_id,
            "norm_query": row["norm_query"],
            "plus_type": row.get("plus_type"),
            "stats": row.get("stats"),
            "bid_kopecks": row.get("bid_kopecks"),
            "updated_at": func.now(),
        }
        for row in clusters
    ]
    stmt = pg_insert(Cluster).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["cabinet_id", "advert_id", "norm_query"],
        set_={
            "plus_type": stmt.excluded.plus_type,
            "stats": stmt.excluded.stats,
            "bid_kopecks": stmt.excluded.bid_kopecks,
            "updated_at": func.now(),
        },
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount)


async def upsert_orders(session: AsyncSession, cabinet_id: int, rows: list[dict]) -> int:
    """Insert or update orders by srid (the PK); metrics refreshed on every pass.

    Rows come from app.collector.orders.parse_orders (each already carries
    cabinet_id). WB refines orders retroactively (cancellations, price
    changes), so every non-key column is re-written on conflict. Duplicates
    of one srid inside a single page are folded first (last one wins) —
    PostgreSQL rejects ON CONFLICT DO UPDATE touching a row twice, and a
    live page MAY repeat a srid (его состояние меняется внутри выгрузки).
    """
    if not rows:
        return 0
    unique: dict[str, dict] = {}
    for row in rows:
        unique[row["srid"]] = row  # last occurrence keeps the freshest state
    values = [{"cabinet_id": cabinet_id, **row} for row in unique.values()]
    stmt = pg_insert(Order).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["srid"],
        set_={
            "nm_id": stmt.excluded.nm_id,
            "date": stmt.excluded.date,
            "hour": stmt.excluded.hour,
            "price_kopecks": stmt.excluded.price_kopecks,
            "date_time": stmt.excluded.date_time,
            "is_cancelled": stmt.excluded.is_cancelled,
        },
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount)


async def log_collection(
    session: AsyncSession,
    cabinet_id: int | None,
    loop_name: str,
    status: str,
    detail: str | None,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> int:
    """Append one collection-loop run to collection_log (cabinet nullable)."""
    result = await session.execute(
        insert(CollectionLog).values(
            cabinet_id=cabinet_id,
            loop_name=loop_name,
            status=status,
            detail=detail,
            started_at=started_at,
            finished_at=finished_at,
        )
    )
    await session.commit()
    return int(result.rowcount)
