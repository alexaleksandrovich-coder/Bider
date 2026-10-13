"""Test fixtures: an isolated Postgres database for model/repository tests.

URL comes from env BIDER_TEST_DATABASE_URL (default
postgresql+asyncpg://app:app@localhost:5436/bider_test — the dev Postgres from
docker-compose.dev.yml must be running). If the database does not exist it is
created (bootstrapping through the "bider" database, which compose always has).

Isolation strategy: per-test savepoints ("join an external transaction" recipe
from the SQLAlchemy docs, async variant). Each test's session is bound to a
connection holding one outer transaction; the test works inside a SAVEPOINT
(recreated automatically after each commit/rollback) and everything is rolled
back when the test ends, so tests never see each other's data and do not
depend on execution order.
"""

import os
from collections.abc import AsyncIterator

from cryptography.fernet import Fernet

TEST_DATABASE_URL = os.environ.get(
    "BIDER_TEST_DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5436/bider_test"
)

# Defaults must be in place before app.* is imported (get_settings() is cached
# and app.db creates its engine at import time).
os.environ.setdefault("BIDER_DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("BIDER_FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("BIDER_ADMIN_USERNAME", "admin")
os.environ.setdefault("BIDER_ADMIN_PASSWORD", "admin")

import pytest_asyncio
from sqlalchemy import (
    event,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.models  # noqa: F401  -- registers all mappers on Base.metadata
from app.db import Base

# The always-present database used to bootstrap the test database if missing.
BOOTSTRAP_DB = "bider"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    url = make_url(TEST_DATABASE_URL)
    admin_engine = create_async_engine(url.set(database=BOOTSTRAP_DB), isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin_engine.dispose()

    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        tx = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        nested = await conn.begin_nested()

        @event.listens_for(session.sync_session, "after_transaction_end")
        def _restart_savepoint(sess, transaction) -> None:
            nonlocal nested
            if not nested.is_active:
                nested = conn.sync_connection.begin_nested()

        yield session
        await session.close()
        await tx.rollback()
