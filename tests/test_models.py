"""Model tests: crypto roundtrip, primary-key/unique constraints, JSONB roundtrip."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.crypto import decrypt_token, encrypt_token
from app.models import Cabinet, Campaign, RawResponse, Session, User

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _add_cabinet(db_session, name: str = "BY-1") -> Cabinet:
    cabinet = Cabinet(
        name=name, wb_type="BY", currency="BYN", token_encrypted=encrypt_token("eyJ...")
    )
    db_session.add(cabinet)
    await db_session.flush()
    return cabinet


async def test_cabinet_token_roundtrip(db_session):
    secret = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.zzz.token"
    cabinet = Cabinet(
        name="BY-1", wb_type="BY", currency="BYN", token_encrypted=encrypt_token(secret)
    )
    db_session.add(cabinet)
    await db_session.flush()

    db_session.expire_all()  # force a fresh SELECT from the database
    loaded = await db_session.scalar(select(Cabinet).where(Cabinet.name == "BY-1"))

    assert loaded is not None
    assert loaded.token_encrypted != secret
    assert decrypt_token(loaded.token_encrypted) == secret


async def test_username_unique(db_session):
    db_session.add(User(username="admin", password_hash="hash1"))
    db_session.add(User(username="admin", password_hash="hash2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_campaign_composite_pk(db_session):
    cabinet = await _add_cabinet(db_session)

    def make_campaign() -> Campaign:
        return Campaign(
            cabinet_id=cabinet.id,
            advert_id=12345,
            name="РК",
            type=8,
            status=9,
            payment_type="cpm",
            bid_type="auto",
            placement_search=True,
            placement_reco=False,
        )

    db_session.add(make_campaign())
    db_session.add(make_campaign())  # same (cabinet_id, advert_id)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_raw_response_jsonb_roundtrip(db_session):
    cabinet = await _add_cabinet(db_session)
    cabinet_id = cabinet.id  # read before expire_all(): expired attr access would lazy-load
    payload = {"a": {"b": [1, 2]}}
    db_session.add(
        RawResponse(
            cabinet_id=cabinet_id,
            endpoint="/adv/v3/fullstats",
            status=200,
            payload=payload,
            captured_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    db_session.expire_all()
    loaded = await db_session.scalar(select(RawResponse).where(RawResponse.cabinet_id == cabinet_id))

    assert loaded is not None
    assert loaded.payload == payload


async def test_session_token_is_pk(db_session):
    user = User(username="u1", password_hash="hash")
    db_session.add(user)
    await db_session.flush()

    expires = datetime.now(UTC) + timedelta(days=1)
    db_session.add(Session(token="tok-1", user_id=user.id, expires_at=expires))
    db_session.add(Session(token="tok-1", user_id=user.id, expires_at=expires))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
