"""Seed-object factories shared by DB-backed tests (queries, web pages).

Each factory returns a NOT-YET-ADDED ORM instance; callers add it to a session
(and commit/flush) themselves. `cabinet_id=None` means "patch it after the
cabinet is flushed" — see tests/web/test_pages.py for the pattern.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.crypto import encrypt_token
from app.models import (
    Balance,
    BidHistory,
    Cabinet,
    Campaign,
    CollectionLog,
    Order,
    Stat,
)

DEFAULT_TOKEN = "x-test-token"

_MSK = ZoneInfo("Europe/Moscow")  # orders date/hour are Moscow time (phase 2)


def make_cabinet(
    *,
    name: str = "Test cabinet",
    wb_type: str = "BY",
    currency: str = "BYN",
    token: str = DEFAULT_TOKEN,
    active: bool = True,
    id: int | None = None,
) -> Cabinet:
    """A Cabinet with an encrypted throwaway token (never plaintext in the DB)."""
    return Cabinet(
        id=id,
        name=name,
        wb_type=wb_type,
        currency=currency,
        token_encrypted=encrypt_token(token),
        active=active,
    )


def make_stat(
    cabinet_id: int | None,
    advert_id: int,
    nm_id: int,
    date: date,
    *,
    app_type: str = "android",
    sum_kopecks: int = 0,
    sum_price_kopecks: int = 0,
    views: int = 0,
    clicks: int = 0,
    atbs: int = 0,
    orders: int = 0,
    shks: int = 0,
    canceled: int = 0,
    cpc_kopecks: int = 0,
) -> Stat:
    return Stat(
        cabinet_id=cabinet_id,
        advert_id=advert_id,
        nm_id=nm_id,
        date=date,
        app_type=app_type,
        views=views,
        clicks=clicks,
        atbs=atbs,
        orders=orders,
        shks=shks,
        canceled=canceled,
        sum_kopecks=sum_kopecks,
        sum_price_kopecks=sum_price_kopecks,
        cpc_kopecks=cpc_kopecks,
    )


def make_campaign(
    cabinet_id: int | None,
    advert_id: int,
    *,
    name: str = "Тестовая РК",
    type: int = 1,
    status: int = 9,
    payment_type: str = "cpc",
    bid_type: str = "auto",
    placement_search: bool = True,
    placement_reco: bool = False,
) -> Campaign:
    return Campaign(
        cabinet_id=cabinet_id,
        advert_id=advert_id,
        name=name,
        type=type,
        status=status,
        payment_type=payment_type,
        bid_type=bid_type,
        placement_search=placement_search,
        placement_reco=placement_reco,
    )


def make_order(
    cabinet_id: int | None,
    srid: str,
    date: date,
    hour: int,
    *,
    nm_id: int = 1,
    price_kopecks: int = 1000,
    is_cancelled: bool = False,
) -> Order:
    """An orders-table row (phase 2): date/hour are MSK, money in kopecks."""
    return Order(
        srid=srid,
        cabinet_id=cabinet_id,
        nm_id=nm_id,
        date=date,
        hour=hour,
        price_kopecks=price_kopecks,
        date_time=datetime(date.year, date.month, date.day, hour, tzinfo=_MSK),
        is_cancelled=is_cancelled,
    )


def make_bid(
    cabinet_id: int | None,
    advert_id: int,
    nm_id: int,
    placement: str,
    bid_kopecks: int,
    captured_at: datetime,
) -> BidHistory:
    return BidHistory(
        cabinet_id=cabinet_id,
        advert_id=advert_id,
        nm_id=nm_id,
        placement=placement,
        bid_kopecks=bid_kopecks,
        captured_at=captured_at,
    )


def make_balance(
    cabinet_id: int | None, total_kopecks: int, captured_at: datetime
) -> Balance:
    return Balance(
        cabinet_id=cabinet_id,
        total_kopecks=total_kopecks,
        captured_at=captured_at,
    )


def make_collection_log(
    cabinet_id: int | None,
    *,
    loop_name: str = "campaigns",
    status: str = "ok",
    detail: str | None = None,
    finished_at: datetime | None = None,
) -> CollectionLog:
    return CollectionLog(
        cabinet_id=cabinet_id,
        loop_name=loop_name,
        status=status,
        detail=detail,
        finished_at=finished_at,
    )
