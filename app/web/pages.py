"""Dashboard pages: overview, one cabinet's campaigns, admin cabinets (Task 8).

All routes are auth-protected via require_user. Money/type/status/freshness
rendering goes through app/web/format.py, registered as Jinja globals so the
templates stay logic-free. Tokens are only ever encrypted (encrypt_token) and
never rendered back.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import encrypt_token
from app.models import Cabinet, User
from app.queries import STATUS_FILTERS, cabinet_campaigns, overview
from app.web.auth import get_db, require_user
from app.web.format import drr, format_money, rel_time, status_name, type_name

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
templates.env.globals.update(
    format_money=format_money,
    drr=drr,
    rel_time=rel_time,
    type_name=type_name,
    status_name=status_name,
)

router = APIRouter(tags=["pages"])

_DbDep = Annotated[AsyncSession, Depends(get_db)]
_UserDep = Annotated[User, Depends(require_user)]

# Collection data older than this (or missing) gets the "stale" alarm class.
_STALE_AFTER = timedelta(minutes=15)

_STATUS_FILTER_LABELS = {
    "active": "Активные",
    "paused": "Пауза",
    "completed": "Завершённые",
    "done": "Отменённые",
    "deleted": "Удалённые",
}


@router.get("/")
async def overview_page(request: Request, user: _UserDep, db: _DbDep):
    """Обзор: per-cabinet spend today/7d/30d, balance, data freshness."""
    rows = await overview(db)
    now = datetime.now(UTC)
    for row in rows:
        freshness = row["freshness"]
        # "нет данных" counts as stale too: absence of data is an alarm.
        row["stale"] = freshness is None or (now - freshness) > _STALE_AFTER
    return templates.TemplateResponse(request, "overview.html", {"user": user, "rows": rows})


@router.get("/cabinets/{cabinet_id}")
async def cabinet_page(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    cabinet_id: int,
    status: str | None = None,
):
    """Список РК кабинета с последними ставками; ?status=<key> фильтрует."""
    cabinet = await db.get(Cabinet, cabinet_id)
    if cabinet is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    # Unknown filter values degrade to "all campaigns" instead of a 500.
    status_key = status if status in STATUS_FILTERS else None
    campaigns = await cabinet_campaigns(db, cabinet_id, status_key)
    return templates.TemplateResponse(
        request,
        "cabinet.html",
        {
            "user": user,
            "cabinet": cabinet,
            "campaigns": campaigns,
            "status": status_key,
            "filter_links": [(key, _STATUS_FILTER_LABELS[key]) for key in STATUS_FILTERS],
        },
    )


@router.get("/admin/cabinets")
async def admin_cabinets_page(request: Request, user: _UserDep, db: _DbDep):
    """Форма добавления кабинета + список подключённых (без токенов)."""
    cabinets = (await db.execute(select(Cabinet).order_by(Cabinet.id))).scalars().all()
    return templates.TemplateResponse(
        request, "admin_cabinets.html", {"user": user, "cabinets": cabinets, "error": None}
    )


@router.post("/admin/cabinets")
async def admin_cabinets_create(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    name: str = Form(...),
    wb_type: str = Form(...),
    token: str = Form(...),
):
    """Add a cabinet: token is stored Fernet-encrypted, currency follows wb_type."""
    name = name.strip()
    token = token.strip()
    if wb_type not in ("BY", "RU"):
        return await _form_error(db, user, request, "wb_type должен быть BY или RU")
    if not name or not token:
        return await _form_error(db, user, request, "Заполните название и токен")
    db.add(
        Cabinet(
            name=name,
            wb_type=wb_type,
            currency="BYN" if wb_type == "BY" else "RUB",
            token_encrypted=encrypt_token(token),
        )
    )
    await db.commit()
    return RedirectResponse("/admin/cabinets", status_code=303)


async def _form_error(db: AsyncSession, user: User, request: Request, error: str):
    """Re-render the admin form with an error message and 422 (no 500s)."""
    cabinets = (await db.execute(select(Cabinet).order_by(Cabinet.id))).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_cabinets.html",
        {"user": user, "cabinets": cabinets, "error": error},
        status_code=422,
    )
