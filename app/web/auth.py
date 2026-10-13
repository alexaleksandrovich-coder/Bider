"""Authentication: scrypt password hashing, DB-backed sessions, admin seeding.

Sessions live in the `sessions` table (models.Session) and travel in an
httpOnly cookie (`bider_session`). `require_user` is a FastAPI dependency:
without a valid (non-expired) session it raises 303 with Location=/login.
"""

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import Session as DbSession
from app.models import User

COOKIE_NAME = "bider_session"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(pw: str) -> str:
    """Hash a password with scrypt; the random salt travels in the string.

    Format: "scrypt$<salt_hex>$<hash_hex>".
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """Check a password against a "scrypt$<salt>$<hash>" string (constant time)."""
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            pw.encode(), salt=bytes.fromhex(salt_hex), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Request-scoped database session for web routes."""
    async with SessionLocal() as db:
        yield db


async def login_user(db: AsyncSession, username: str, password: str) -> str | None:
    """Verify credentials and create a session; return its token or None."""
    user = await db.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        return None
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=get_settings().session_days)
    db.add(DbSession(token=token, user_id=user.id, expires_at=expires_at))
    await db.commit()
    return token


async def logout_user(db: AsyncSession, token: str) -> None:
    """Delete the session row (if any) — the cookie becomes worthless."""
    session = await db.get(DbSession, token)
    if session is not None:
        await db.delete(session)
        await db.commit()


async def require_user(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> User:
    """FastAPI dependency: the current User or a 303 redirect to /login.

    FastAPI renders HTTPException through its JSON handler, so the 303 keeps
    the Location header (client libraries and browsers follow it) while the
    body is a tiny JSON error instead of a full HTML page.
    """
    token = request.cookies.get(COOKIE_NAME)
    if token:
        row = (
            await db.execute(select(DbSession).where(DbSession.token == token))
        ).first()
        if row is not None:
            session = row[0]
            if session.expires_at > datetime.now(UTC):
                user = await db.get(User, session.user_id)
                if user is not None:
                    return user
            await db.delete(session)  # expired: drop it
            await db.commit()
    raise HTTPException(status_code=303, detail="Not authenticated", headers={"Location": "/login"})


async def seed_admin() -> None:
    """Create the admin user from Settings if the users table is empty."""
    async with SessionLocal() as db:
        count = await db.scalar(select(func.count()).select_from(User))
        if count:
            return
        settings = get_settings()
        db.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
            )
        )
        await db.commit()
