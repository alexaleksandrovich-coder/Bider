"""Read-side queries for the dashboard.

Semantics:
- stats.date is already the Moscow-calendar date (the collector stores it that
  way), so "today / 7d / 30d" compare against the current Europe/Moscow date.
  7d/30d are the last N days inclusive of today.
- Money stays integer kopecks of the cabinet's currency; formatting lives in
  app/web/format.py.
- balance: the latest balances.total_kopecks by captured_at (None if never).
- freshness: max(finished_at) of successful ("ok") collection_log runs of the
  cabinet (None if there are none).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuctionSnapshot,
    Balance,
    BidHistory,
    Budget,
    Cabinet,
    Campaign,
    Cluster,
    CollectionLog,
    Order,
    Position,
    Stat,
)
from app.web.format import drr

_MSK = ZoneInfo("Europe/Moscow")  # WB reporting timezone: UTC+3, no DST

# URL status filter -> campaigns.status code (see app/web/format.py STATUS_NAMES).
STATUS_FILTERS: dict[str, int] = {
    "active": 9,
    "paused": 11,
    "completed": 7,
    "done": 8,
    "deleted": -1,
}


def msk_today() -> date:
    """Current calendar date in Moscow time."""
    return datetime.now(_MSK).date()


async def overview(session: AsyncSession) -> list[dict]:
    """One overview row per active cabinet, ordered by cabinet id.

    Row: {cabinet: Cabinet, spent_today/spent_7d/spent_30d: int,
    balance: int | None, freshness: datetime | None}.
    """
    today = msk_today()
    from_7d = today - timedelta(days=6)
    from_30d = today - timedelta(days=29)

    cabinets = (
        await session.execute(
            select(Cabinet).where(Cabinet.active.is_(True)).order_by(Cabinet.id)
        )
    ).scalars().all()

    spend_rows = (
        await session.execute(
            select(
                Stat.cabinet_id,
                func.coalesce(func.sum(Stat.sum_kopecks).filter(Stat.date == today), 0).label("today"),
                func.coalesce(func.sum(Stat.sum_kopecks).filter(Stat.date >= from_7d), 0).label("last7"),
                func.coalesce(func.sum(Stat.sum_kopecks).filter(Stat.date >= from_30d), 0).label("last30"),
            ).group_by(Stat.cabinet_id)
        )
    ).all()
    spend = {row.cabinet_id: row for row in spend_rows}

    # Latest balance per cabinet: rank by captured_at, keep rank 1.
    balance_ranked = (
        select(
            Balance.cabinet_id,
            Balance.total_kopecks,
            func.row_number()
            .over(
                partition_by=Balance.cabinet_id,
                order_by=[Balance.captured_at.desc(), Balance.id.desc()],
            )
            .label("rn"),
        ).subquery()
    )
    balances = {
        row.cabinet_id: row.total_kopecks
        for row in (
            await session.execute(
                select(balance_ranked.c.cabinet_id, balance_ranked.c.total_kopecks).where(
                    balance_ranked.c.rn == 1
                )
            )
        ).all()
    }

    freshness_rows = (
        await session.execute(
            select(CollectionLog.cabinet_id, func.max(CollectionLog.finished_at).label("last_ok"))
            .where(CollectionLog.status == "ok", CollectionLog.cabinet_id.is_not(None))
            .group_by(CollectionLog.cabinet_id)
        )
    ).all()
    freshness = {row.cabinet_id: row.last_ok for row in freshness_rows}

    return [
        {
            "cabinet": cabinet,
            "spent_today": int(spend[cabinet.id].today) if cabinet.id in spend else 0,
            "spent_7d": int(spend[cabinet.id].last7) if cabinet.id in spend else 0,
            "spent_30d": int(spend[cabinet.id].last30) if cabinet.id in spend else 0,
            "balance": balances.get(cabinet.id),
            "freshness": freshness.get(cabinet.id),
        }
        for cabinet in cabinets
    ]


async def cabinet_campaigns(
    session: AsyncSession, cabinet_id: int, status: str | None = None
) -> list[dict]:
    """All campaigns of one cabinet, sorted by advert_id.

    status is a URL filter key from STATUS_FILTERS (None = every campaign).
    Each row additionally carries bid_search / bid_reco — the most recent
    bid_history observation per placement (latest captured_at, None if the
    placement was never observed).
    """
    stmt = select(Campaign).where(Campaign.cabinet_id == cabinet_id)
    if status is not None:
        if status not in STATUS_FILTERS:
            raise ValueError(f"unknown status filter: {status!r}")
        stmt = stmt.where(Campaign.status == STATUS_FILTERS[status])
    stmt = stmt.order_by(Campaign.advert_id)
    campaigns = (await session.execute(stmt)).scalars().all()

    # Latest known bid per (advert, placement) within this cabinet.
    rn = (
        func.row_number()
        .over(
            partition_by=[BidHistory.advert_id, BidHistory.placement],
            order_by=[BidHistory.captured_at.desc(), BidHistory.id.desc()],
        )
        .label("rn")
    )
    ranked = (
        select(BidHistory.advert_id, BidHistory.placement, BidHistory.bid_kopecks, rn)
        .where(BidHistory.cabinet_id == cabinet_id)
        .subquery()
    )
    bids: dict[tuple[int, str], int] = {}
    for row in await session.execute(
        select(ranked.c.advert_id, ranked.c.placement, ranked.c.bid_kopecks).where(ranked.c.rn == 1)
    ):
        bids[(row.advert_id, row.placement)] = row.bid_kopecks

    return [
        {
            "advert_id": campaign.advert_id,
            "name": campaign.name,
            "type": campaign.type,
            "status": campaign.status,
            "payment_type": campaign.payment_type,
            "bid_type": campaign.bid_type,
            "placement_search": campaign.placement_search,
            "placement_reco": campaign.placement_reco,
            "bid_search": bids.get((campaign.advert_id, "search")),
            "bid_reco": bids.get((campaign.advert_id, "recommendations")),
        }
        for campaign in campaigns
    ]


_VALID_DETAIL_DAYS = (7, 30)


async def campaign_detail(
    session: AsyncSession, cabinet_id: int, advert_id: int, days: int
) -> dict:
    """Everything the campaign detail page needs for the last `days` days.

    days must be 7 or 30 (ValueError otherwise — the page route turns that
    into a 422). The window is [today-days+1 .. today] by the Moscow calendar,
    same semantics as overview's 7d/30d buckets. Keys of the returned dict:

    - campaign: the Campaign ORM row (None when there is no such campaign)
    - totals: sums over the window aggregated across nms and app_types
      {views, clicks, orders, shks, sum_kopecks, sum_price_kopecks}
    - per_nm: the same sums per product, sorted by sum_kopecks desc, each row
      with drr (None when the product had no revenue)
    - bid_series: bid_history points, chronological
      [{"ts": captured_at.isoformat(), "placement", "bid_kopecks"}]
    - position_series: per-date average position
      [{"date": "YYYY-MM-DD", "avg": float}]
    - spend_series: one entry per window day, NO holes: days without stats
      carry sum_kopecks=None [{"date": "YYYY-MM-DD", "sum_kopecks": int|None}]
    - budget: latest budgets capture (by captured_at)
      {"cash", "netting", "total"} or None when never collected
    - clusters: manual-bid phrases sorted by norm_query
    - snapshots_last: latest auction snapshots (max 5), newest first
    """
    if days not in _VALID_DETAIL_DAYS:
        raise ValueError(f"days must be one of {_VALID_DETAIL_DAYS}, got {days!r}")
    today = msk_today()
    from_day = today - timedelta(days=days - 1)

    campaign = await session.get(Campaign, (cabinet_id, advert_id))

    in_window = (
        Stat.cabinet_id == cabinet_id,
        Stat.advert_id == advert_id,
        Stat.date >= from_day,
        Stat.date <= today,
    )
    per_nm_rows = (
        await session.execute(
            select(
                Stat.nm_id,
                func.sum(Stat.views).label("views"),
                func.sum(Stat.clicks).label("clicks"),
                func.sum(Stat.orders).label("orders"),
                func.sum(Stat.shks).label("shks"),
                func.sum(Stat.sum_kopecks).label("sum_kopecks"),
                func.sum(Stat.sum_price_kopecks).label("sum_price_kopecks"),
            )
            .where(*in_window)
            .group_by(Stat.nm_id)
        )
    ).all()
    totals = {
        key: sum(int(getattr(row, key)) for row in per_nm_rows)
        for key in ("views", "clicks", "orders", "shks", "sum_kopecks", "sum_price_kopecks")
    }
    per_nm = [
        {
            "nm_id": row.nm_id,
            "views": int(row.views),
            "clicks": int(row.clicks),
            "orders": int(row.orders),
            "shks": int(row.shks),
            "sum_kopecks": int(row.sum_kopecks),
            "sum_price_kopecks": int(row.sum_price_kopecks),
            "drr": drr(int(row.sum_kopecks), int(row.sum_price_kopecks)),
        }
        for row in sorted(per_nm_rows, key=lambda r: (-int(r.sum_kopecks), r.nm_id))
    ]

    # Daily spend, padded to the full window so chart axes have no gaps.
    spend_by_date = {
        row.date: int(row.total)
        for row in (
            await session.execute(
                select(Stat.date, func.sum(Stat.sum_kopecks).label("total"))
                .where(*in_window)
                .group_by(Stat.date)
            )
        ).all()
    }
    spend_series = [
        {
            "date": (from_day + timedelta(days=offset)).isoformat(),
            "sum_kopecks": spend_by_date.get(from_day + timedelta(days=offset)),
        }
        for offset in range(days)
    ]

    bids = (
        (
            await session.execute(
                select(BidHistory)
                .where(
                    BidHistory.cabinet_id == cabinet_id,
                    BidHistory.advert_id == advert_id,
                )
                .order_by(BidHistory.captured_at, BidHistory.id)
            )
        )
        .scalars()
        .all()
    )
    bid_series = [
        {
            "ts": bid.captured_at.isoformat(),
            "placement": bid.placement,
            "bid_kopecks": bid.bid_kopecks,
        }
        for bid in bids
    ]

    position_rows = (
        await session.execute(
            select(Position.date, func.avg(Position.avg_position).label("avg"))
            .where(
                Position.cabinet_id == cabinet_id,
                Position.advert_id == advert_id,
                Position.date >= from_day,
                Position.date <= today,
            )
            .group_by(Position.date)
            .order_by(Position.date)
        )
    ).all()
    position_series = [
        {"date": row.date.isoformat(), "avg": float(row.avg)} for row in position_rows
    ]

    budget_row = (
        await session.execute(
            select(Budget)
            .where(Budget.cabinet_id == cabinet_id, Budget.advert_id == advert_id)
            .order_by(Budget.captured_at.desc(), Budget.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    budget = (
        {
            "cash": budget_row.cash_kopecks,
            "netting": budget_row.netting_kopecks,
            "total": budget_row.total_kopecks,
        }
        if budget_row is not None
        else None
    )

    clusters = [
        {
            "norm_query": cluster.norm_query,
            "plus_type": cluster.plus_type,
            "stats": cluster.stats,
            "bid_kopecks": cluster.bid_kopecks,
            "updated_at": cluster.updated_at,
        }
        for cluster in (
            (
                await session.execute(
                    select(Cluster)
                    .where(
                        Cluster.cabinet_id == cabinet_id,
                        Cluster.advert_id == advert_id,
                    )
                    .order_by(Cluster.norm_query)
                )
            )
            .scalars()
            .all()
        )
    ]

    snapshots_last = [
        {"captured_at": snap.captured_at, "payload": snap.payload, "parsed": snap.parsed}
        for snap in (
            (
                await session.execute(
                    select(AuctionSnapshot)
                    .where(
                        AuctionSnapshot.cabinet_id == cabinet_id,
                        AuctionSnapshot.advert_id == advert_id,
                    )
                    .order_by(AuctionSnapshot.captured_at.desc(), AuctionSnapshot.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    ]

    return {
        "campaign": campaign,
        "days": days,
        "from_day": from_day,
        "to_day": today,
        "totals": totals,
        "per_nm": per_nm,
        "bid_series": bid_series,
        "position_series": position_series,
        "spend_series": spend_series,
        "budget": budget,
        "clusters": clusters,
        "snapshots_last": snapshots_last,
    }


async def items_summary(session: AsyncSession, cabinet_id: int) -> list[dict]:
    """Per-product totals across ALL campaigns of one cabinet.

    Rows {nm_id, spent, revenue, orders, shks, drr} sorted by spent desc
    (nm_id as the tiebreaker); drr is None when the product has no revenue.
    """
    rows = (
        await session.execute(
            select(
                Stat.nm_id,
                func.coalesce(func.sum(Stat.sum_kopecks), 0).label("spent"),
                func.coalesce(func.sum(Stat.sum_price_kopecks), 0).label("revenue"),
                func.coalesce(func.sum(Stat.orders), 0).label("orders"),
                func.coalesce(func.sum(Stat.shks), 0).label("shks"),
            )
            .where(Stat.cabinet_id == cabinet_id)
            .group_by(Stat.nm_id)
        )
    ).all()
    items = [
        {
            "nm_id": row.nm_id,
            "spent": int(row.spent),
            "revenue": int(row.revenue),
            "orders": int(row.orders),
            "shks": int(row.shks),
            "drr": drr(int(row.spent), int(row.revenue)),
        }
        for row in rows
    ]
    items.sort(key=lambda item: (-item["spent"], item["nm_id"]))
    return items


async def hours_heatmap(session: AsyncSession, cabinet_id: int, days: int) -> list[dict]:
    """Orders and revenue per MSK hour for the last `days` days.

    The window is [today-days+1 .. today] by the Moscow calendar, same
    semantics as overview's 7d/30d buckets. `hour` is the stored MSK hour of
    the order (approximated by lastChangeDate at collection time); cancelled
    orders are excluded from both counts and revenue. The result always
    covers all 24 hours sorted 0..23 (zero hours included) so chart axes
    never have gaps: [{hour, orders: int, revenue_kopecks: int}].
    """
    today = msk_today()
    from_day = today - timedelta(days=days - 1)
    by_hour: dict[int, tuple[int, int]] = {
        row.hour: (int(row.orders), int(row.revenue))
        for row in (
            await session.execute(
                select(
                    Order.hour,
                    func.count().label("orders"),
                    func.coalesce(func.sum(Order.price_kopecks), 0).label("revenue"),
                )
                .where(
                    Order.cabinet_id == cabinet_id,
                    Order.is_cancelled.is_(False),
                    Order.date >= from_day,
                    Order.date <= today,
                )
                .group_by(Order.hour)
            )
        ).all()
    }
    return [
        {
            "hour": hour,
            "orders": by_hour.get(hour, (0, 0))[0],
            "revenue_kopecks": by_hour.get(hour, (0, 0))[1],
        }
        for hour in range(24)
    ]


async def health_status(session: AsyncSession) -> dict:
    """Collector health: the last run per (cabinet, loop) + recent log lines.

    Per-campaign loop names ("fullstats:123") are grouped under their base
    name ("fullstats"): the newest row by finished_at represents the loop.
    "loops" rows: {cabinet_id (None for cabinetless loops), loop_name,
    last_status, last_finished_at, detail}, ordered by cabinet then name.
    "log": the last 100 collection_log rows, newest first.
    """
    base_name = func.split_part(CollectionLog.loop_name, ":", 1).label("base")
    rn = (
        func.row_number()
        .over(
            partition_by=[CollectionLog.cabinet_id, base_name],
            order_by=[CollectionLog.finished_at.desc().nullslast(), CollectionLog.id.desc()],
        )
        .label("rn")
    )
    ranked = select(
        CollectionLog.cabinet_id,
        base_name,
        CollectionLog.status,
        CollectionLog.detail,
        CollectionLog.finished_at,
        rn,
    ).subquery()
    loop_rows = (
        await session.execute(
            select(
                ranked.c.cabinet_id,
                ranked.c.base,
                ranked.c.status,
                ranked.c.detail,
                ranked.c.finished_at,
            ).where(ranked.c.rn == 1)
        )
    ).all()
    loops = [
        {
            "cabinet_id": row.cabinet_id,
            "loop_name": row.base,
            "last_status": row.status,
            "last_finished_at": row.finished_at,
            "detail": row.detail,
        }
        for row in loop_rows
    ]
    loops.sort(
        key=lambda row: (row["cabinet_id"] is None, row["cabinet_id"] or 0, row["loop_name"])
    )

    log_rows = (
        (
            await session.execute(
                select(CollectionLog).order_by(CollectionLog.id.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    return {"loops": loops, "log": list(log_rows)}
