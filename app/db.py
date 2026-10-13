"""Async engine, session factory and the declarative base.

`Base` lives here so `app.models` and Alembic's env.py can import it without
circular imports. `engine` / `SessionLocal` are module-level singletons built
from `Settings.database_url` (env prefix BIDER_).
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base; its metadata is populated by importing app.models."""


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def dispose_engine() -> None:
    """Dispose the engine's connection pool (call on application shutdown)."""
    await engine.dispose()
