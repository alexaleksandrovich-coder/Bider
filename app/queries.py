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

from app.models import Balance, BidHistory, Cabinet, Campaign, CollectionLog, Stat

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
