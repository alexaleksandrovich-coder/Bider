"""SQLAlchemy models.

Table and column names follow .scratch/spec.md, «Implementation Decisions →
Модель данных» — that section is the single source of truth for the schema.
All timestamps are timezone-aware; `captured_at` is UTC, stat `date` is a
calendar date in Moscow time (UTC+3). Money is integer kopecks (*_kopecks).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

_BIGSERIAL_PK = {"primary_key": True, "autoincrement": True}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Cabinet(Base):
    __tablename__ = "cabinets"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    wb_type: Mapped[str] = mapped_column(String(2), nullable=False)  # "BY" | "RU"
    currency: Mapped[str] = mapped_column(String(3), nullable=False)  # "BYN" | "RUB"
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), primary_key=True
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_type: Mapped[str] = mapped_column(Text, nullable=False)
    bid_type: Mapped[str] = mapped_column(Text, nullable=False)
    placement_search: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    placement_reco: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    change_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BidHistory(Base):
    """Append-only: one row per observed bid, never updated."""

    __tablename__ = "bid_history"
    __table_args__ = (
        Index(
            "ix_bid_history_cabinet_advert_nm_captured",
            "cabinet_id",
            "advert_id",
            "nm_id",
            "captured_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nm_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    placement: Mapped[str] = mapped_column(Text, nullable=False)  # "search" | "recommendations"
    bid_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Stat(Base):
    """Daily per-product stats, upserted from fullstats days[] (per appType)."""

    __tablename__ = "stats"

    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), primary_key=True
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    app_type: Mapped[str] = mapped_column(Text, primary_key=True)

    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    atbs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canceled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sum_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sum_price_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cpc_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class Position(Base):
    """Average position per product/day, from fullstats boosterStats."""

    __tablename__ = "positions"

    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), primary_key=True
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    avg_position: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        Index("ix_budgets_cabinet_advert_captured", "cabinet_id", "advert_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cash_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    netting_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Balance(Base):
    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    total_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuctionSnapshot(Base):
    """Raw auction snapshot (recommendations response) per campaign."""

    __tablename__ = "auction_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    parsed: Mapped[dict | None] = mapped_column(JSONB)


class Cluster(Base):
    __tablename__ = "clusters"
    __table_args__ = (
        UniqueConstraint(
            "cabinet_id", "advert_id", "norm_query", name="uq_clusters_cabinet_advert_norm_query"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    norm_query: Mapped[str] = mapped_column(Text, nullable=False)
    plus_type: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict | None] = mapped_column(JSONB)
    bid_kopecks: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionLog(Base):
    """One row per collection-loop run; cabinet is nullable for cabinetless
    loops (e.g. the nightly raw_responses cleaner)."""

    __tablename__ = "collection_log"

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int | None] = mapped_column(ForeignKey("cabinets.id", ondelete="CASCADE"))
    loop_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawResponse(Base):
    """Raw API responses kept for debugging; retention handled by a nightly cleanup."""

    __tablename__ = "raw_responses"
    __table_args__ = (Index("ix_raw_responses_captured_at", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Order(Base):
    """One WB order row (statistics-api supplier/orders), deduplicated by srid.

    `srid` is WB's free-form order id (text, unlimited length); money is
    integer kopecks (finishedPrice * 100); date/hour are Moscow time.
    """

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_cabinet_date_hour", "cabinet_id", "date", "hour"),)

    srid: Mapped[str] = mapped_column(Text, primary_key=True)
    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), nullable=False
    )
    nm_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    hour: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    price_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    date_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Schedule(Base):
    """Per-campaign showing schedule approved via the UI: 24×0/1 hours (MSK)."""

    __tablename__ = "schedules"

    cabinet_id: Mapped[int] = mapped_column(
        ForeignKey("cabinets.id", ondelete="CASCADE"), primary_key=True
    )
    advert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hours: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    daily_limit_kopecks: Mapped[int | None] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ControllerLog(Base):
    """One row per controller action (pause/start/limit); dry_run logs status="dry"."""

    __tablename__ = "controller_log"

    id: Mapped[int] = mapped_column(BigInteger, **_BIGSERIAL_PK)  # bigserial
    cabinet_id: Mapped[int | None] = mapped_column(ForeignKey("cabinets.id", ondelete="CASCADE"))
    advert_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # "pause" | "start" | "limit"
    status: Mapped[str] = mapped_column(Text, nullable=False)  # "ok" | "error" | "dry" | "skipped"
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AppSetting(Base):
    """Key/value settings (controller_mode, controller_kill_switch, ...)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
