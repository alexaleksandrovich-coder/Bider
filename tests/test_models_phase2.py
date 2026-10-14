"""Phase-2 model tests: orders, schedules, controller_log, app_settings.

Roundtrips (including the 24-element JSONB hours list) and primary-key /
index violations, mirroring tests/test_models.py.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.crypto import encrypt_token
from app.models import AppSetting, Cabinet, ControllerLog, Order, Schedule

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _add_cabinet(db_session, name: str = "BY-P2") -> Cabinet:
    cabinet = Cabinet(
        name=name, wb_type="BY", currency="BYN", token_encrypted=encrypt_token("eyJ...")
    )
    db_session.add(cabinet)
    await db_session.flush()
    return cabinet


async def test_order_roundtrip(db_session):
    cabinet_id = (await _add_cabinet(db_session)).id
    srid = "ei.i1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0.0.0"  # live shape: free-form text
    db_session.add(
        Order(
            srid=srid,
            cabinet_id=cabinet_id,
            nm_id=222604009,
            date=date(2026, 10, 12),
            hour=21,
            price_kopecks=35500,  # finishedPrice 355 units -> *100
            date_time=datetime(2026, 10, 12, 21, 21, 39, tzinfo=UTC),
            is_cancelled=False,
        )
    )
    await db_session.flush()

    db_session.expire_all()  # force a fresh SELECT from the database
    loaded = await db_session.get(Order, srid)

    assert loaded is not None
    assert loaded.cabinet_id == cabinet_id
    assert loaded.nm_id == 222604009
    assert loaded.date == date(2026, 10, 12)
    assert loaded.hour == 21
    assert loaded.price_kopecks == 35500
    assert loaded.date_time == datetime(2026, 10, 12, 21, 21, 39, tzinfo=UTC)
    assert loaded.is_cancelled is False


async def test_order_srid_is_pk_duplicate_fails(db_session):
    cabinet_id = (await _add_cabinet(db_session)).id

    def make_order() -> Order:
        return Order(
            srid="ei.idup.0.0",
            cabinet_id=cabinet_id,
            nm_id=1,
            date=date(2026, 10, 12),
            hour=10,
            price_kopecks=100,
            date_time=datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC),
        )

    db_session.add(make_order())
    db_session.add(make_order())  # same srid
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_orders_has_cabinet_date_hour_index(db_session):
    def _index_names(sync_session):
        inspector = inspect(sync_session.get_bind())
        return {ix["name"] for ix in inspector.get_indexes("orders")}

    names = await db_session.run_sync(_index_names)

    assert "ix_orders_cabinet_date_hour" in names


async def test_schedule_roundtrip_and_defaults(db_session):
    cabinet_id = (await _add_cabinet(db_session)).id
    hours = [1 if h in (10, 11, 21) else 0 for h in range(24)]
    db_session.add(
        Schedule(
            cabinet_id=cabinet_id,
            advert_id=12345,
            hours=hours,
            daily_limit_kopecks=150000,
        )
    )
    await db_session.flush()

    db_session.expire_all()  # reload: server defaults become visible
    loaded = await db_session.get(Schedule, (cabinet_id, 12345))

    assert loaded is not None
    assert loaded.hours == hours  # JSONB list roundtrip, 24 elements
    assert loaded.daily_limit_kopecks == 150000
    assert loaded.enabled is False  # python-side default
    assert loaded.updated_at is not None  # server_default now()
    assert loaded.applied_at is None


async def test_schedule_hours_jsonb_roundtrip_24_elements(db_session):
    cabinet_id = (await _add_cabinet(db_session)).id
    hours = [h % 2 for h in range(24)]
    db_session.add(Schedule(cabinet_id=cabinet_id, advert_id=1, hours=hours))
    await db_session.flush()

    db_session.expire_all()
    loaded = await db_session.get(Schedule, (cabinet_id, 1))

    assert loaded is not None
    assert len(loaded.hours) == 24
    assert loaded.hours == [0, 1] * 12


async def test_schedule_composite_pk_duplicate_fails(db_session):
    cabinet_id = (await _add_cabinet(db_session)).id
    hours = [0] * 24

    def make_schedule() -> Schedule:
        return Schedule(cabinet_id=cabinet_id, advert_id=777, hours=hours)

    db_session.add(make_schedule())
    db_session.add(make_schedule())  # same (cabinet_id, advert_id)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_controller_log_roundtrip(db_session):
    cabinet_id = (await _add_cabinet(db_session)).id
    created = datetime.now(UTC) - timedelta(seconds=1)
    db_session.add(
        ControllerLog(
            cabinet_id=cabinet_id,
            advert_id=12345,
            action="pause",
            status="dry",
            detail="hour 3 not in schedule",
        )
    )
    await db_session.flush()

    db_session.expire_all()
    loaded = await db_session.scalar(
        select(ControllerLog).where(ControllerLog.cabinet_id == cabinet_id)
    )

    assert loaded is not None
    assert loaded.advert_id == 12345
    assert loaded.action == "pause"
    assert loaded.status == "dry"
    assert loaded.detail == "hour 3 not in schedule"
    assert loaded.created_at is not None
    assert loaded.created_at > created  # server_default now()


async def test_controller_log_nullable_cabinet_and_advert(db_session):
    db_session.add(ControllerLog(action="limit", status="skipped"))
    await db_session.flush()

    db_session.expire_all()
    loaded = await db_session.scalar(select(ControllerLog))

    assert loaded is not None
    assert loaded.cabinet_id is None
    assert loaded.advert_id is None


async def test_app_settings_roundtrip(db_session):
    db_session.add(AppSetting(key="controller_mode", value={"mode": "dry_run"}))
    await db_session.flush()

    db_session.expire_all()
    loaded = await db_session.get(AppSetting, "controller_mode")

    assert loaded is not None
    assert loaded.value == {"mode": "dry_run"}


async def test_app_settings_key_is_pk_duplicate_fails(db_session):
    db_session.add(AppSetting(key="controller_kill_switch", value={"on": True}))
    db_session.add(AppSetting(key="controller_kill_switch", value={"on": False}))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
