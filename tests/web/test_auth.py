"""Auth flow tests: password hashing, login/logout, seed admin, expired sessions.

The web routes persist through app.db.SessionLocal, which points at the test
database (tests/conftest.py pins BIDER_DATABASE_URL before any app import).
Direct DB assertions run on a throwaway engine + event loop (asyncio.run) so
the app's module-level connection pool is never shared across loops.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Session as DbSession
from app.models import User
from app.web.auth import COOKIE_NAME, hash_password, verify_password


def _run_db(coro) -> object:
    """Run a `async def coro(db)` against a throwaway engine, return its result."""

    async def _with_engine():
        engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await coro(db)
        finally:
            await engine.dispose()

    return asyncio.run(_with_engine())


def _login(client: TestClient) -> None:
    s = get_settings()
    r = client.post(
        "/login", data={"username": s.admin_username, "password": s.admin_password}
    )
    assert r.status_code == 200  # followed the 303 to "/"


# --- password hashing -------------------------------------------------------


def test_password_hash_roundtrip():
    stored = hash_password("s3cret")
    assert stored.startswith("scrypt$")
    assert verify_password("s3cret", stored)
    assert not verify_password("wrong", stored)
    # malformed stored values must not raise, just fail verification
    assert not verify_password("s3cret", "garbage")
    assert not verify_password("s3cret", "md5$aabb$ccdd")


# --- redirects and pages ----------------------------------------------------


def test_unauthenticated_root_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_has_post_form(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'action="/login"' in r.text
    assert 'method="post"' in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_login_success_sets_session_cookie(client):
    s = get_settings()
    r = client.post(
        "/login",
        data={"username": s.admin_username, "password": s.admin_password},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert COOKIE_NAME in client.cookies


def test_login_wrong_password_shows_error_without_cookie(client):
    s = get_settings()
    r = client.post(
        "/login",
        data={"username": s.admin_username, "password": "definitely-wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=1"
    assert COOKIE_NAME not in client.cookies
    page = client.get("/login", params={"error": "1"})
    assert page.status_code == 200
    assert "Неверный логин или пароль" in page.text


def test_authenticated_root_shows_overview(client):
    _login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "Обзор" in r.text


# --- logout -----------------------------------------------------------------


def test_logout_invalidates_session(client):
    _login(client)
    token = client.cookies[COOKIE_NAME]

    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    # replay the old cookie on a clean jar: it must no longer grant access
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token)
    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/login"


# --- seed admin -------------------------------------------------------------


def test_seed_admin_after_startup(client):
    s = get_settings()

    async def check(db):
        return (await db.execute(select(User))).scalars().all()

    users = _run_db(check)
    assert len(users) == 1
    assert users[0].username == s.admin_username
    assert verify_password(s.admin_password, users[0].password_hash)


# --- session expiry ---------------------------------------------------------


def test_expired_session_is_rejected(client):
    s = get_settings()
    token = "expired-token-123"

    async def insert_expired(db):
        user = await db.scalar(select(User).where(User.username == s.admin_username))
        assert user is not None
        db.add(
            DbSession(
                token=token,
                user_id=user.id,
                expires_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await db.commit()

    _run_db(insert_expired)

    client.cookies.set(COOKIE_NAME, token)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
