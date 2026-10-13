"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, dispose_engine
from app.models import User
from app.web.auth import (
    COOKIE_NAME,
    get_db,
    login_user,
    logout_user,
    require_user,
    seed_admin,
)

VERSION = "0.1.0"

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Seed the admin user, then — with run_collector=True — start the
        read-only collection loops for the process lifetime; otherwise the
        app behaves exactly as before."""
        await seed_admin()
        loops = []
        if get_settings().run_collector:
            from app.collector.runner import start_all, stop_all

            loops = await start_all()
        yield
        if loops:
            await stop_all(loops)
        await dispose_engine()

    app = FastAPI(title="bider", version=VERSION, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "version": VERSION}

    @app.get("/login")
    async def login_page(request: Request, error: str | None = None) -> RedirectResponse:
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    async def login_submit(username: str = Form(...), password: str = Form(...)):
        async with SessionLocal() as db:
            token = await login_user(db, username, password)
        if token is None:
            return RedirectResponse("/login?error=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            max_age=get_settings().session_days * 86400,
        )
        return response

    @app.post("/logout")
    async def logout(request: Request, db: Annotated[AsyncSession, Depends(get_db)]):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            await logout_user(db, token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # Placeholder for Task 8 (dashboard overview); auth-protected.
    @app.get("/")
    async def overview(
        request: Request,
        user: Annotated[User, Depends(require_user)],
    ):
        return templates.TemplateResponse(request, "overview.html", {"user": user})

    return app
