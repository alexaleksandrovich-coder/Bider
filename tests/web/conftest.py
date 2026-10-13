"""Fixtures for web tests.

tests/conftest.py already pins BIDER_DATABASE_URL / admin credentials via
os.environ.setdefault BEFORE any app import, so app.db.SessionLocal — used by
the web routes — points at the isolated bider_test database. This conftest
only guarantees the schema exists (for web-only runs) and hands out a
TestClient that runs the full lifespan (seed admin included).
"""

import asyncio
import os

os.environ.setdefault("BIDER_ADMIN_USERNAME", "admin")
os.environ.setdefault("BIDER_ADMIN_PASSWORD", "admin")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture(scope="session")
def web_schema() -> None:
    """Create tables if they do not exist yet (create_all is idempotent).

    In a full-suite run tests/conftest.py's engine fixture has already
    dropped/recreated the schema; in a web-only run nothing else did.
    """

    async def _create() -> None:
        import app.models  # noqa: F401  -- registers all mappers on Base.metadata
        from app.db import Base

        engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(_create())


@pytest.fixture
def client(web_schema: None):
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client):
    """TestClient logged in as the seeded admin (Settings credentials)."""
    from app.config import get_settings

    settings = get_settings()
    response = client.post(
        "/login", data={"username": settings.admin_username, "password": settings.admin_password}
    )
    assert response.status_code == 200  # followed the 303 to "/"
    return client
