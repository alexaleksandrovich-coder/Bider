"""FastAPI application factory."""

from fastapi import FastAPI

VERSION = "0.1.0"


def create_app() -> FastAPI:
    app = FastAPI(title="bider", version=VERSION)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "version": VERSION}

    return app
