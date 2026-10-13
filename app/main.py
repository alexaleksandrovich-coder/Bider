"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings

VERSION = "0.1.0"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """With run_collector=True, start the read-only collection loops for
        the process lifetime; otherwise the app behaves exactly as before."""
        loops = []
        if get_settings().run_collector:
            from app.collector.runner import start_all, stop_all

            loops = await start_all()
        yield
        if loops:
            await stop_all(loops)

    app = FastAPI(title="bider", version=VERSION, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "version": VERSION}

    return app
