"""Dashboard pages: overview, one cabinet's campaigns, admin cabinets (Task 8)
plus campaign detail, items summary and health (Task 9).

All routes are auth-protected via require_user. Money/type/status/freshness
rendering goes through app/web/format.py, registered as Jinja globals so the
templates stay logic-free. Tokens are only ever encrypted (encrypt_token) and
never rendered back.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.runner import LOOPS_SPEC, stop_for_cabinet, sync_cabinet_once
from app.crypto import encrypt_token
from app.models import Cabinet, CollectionLog, User
from app.queries import (
    STATUS_FILTERS,
    cabinet_campaigns,
    campaign_detail,
    health_status,
    hours_heatmap,
    items_summary,
    overview,
)
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
    # Cabinets without a single collection_log row have never been collected:
    # the card explains when to expect data instead of a bare "нет данных".
    log_counts = dict(
        (
            await db.execute(
                select(CollectionLog.cabinet_id, func.count())
                .where(CollectionLog.cabinet_id.is_not(None))
                .group_by(CollectionLog.cabinet_id)
            )
        ).all()
    )
    for row in rows:
        freshness = row["freshness"]
        # "нет данных" counts as stale too: absence of data is an alarm.
        row["stale"] = freshness is None or (now - freshness) > _STALE_AFTER
        row["never_collected"] = log_counts.get(row["cabinet"].id, 0) == 0
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


# Human labels for the last collection-run badge (statuses written by IntervalLoop).
_RUN_STATUS_LABELS = {
    "ok": "ok",
    "error": "ошибка",
    "paused": "пауза",
    "forbidden": "токен отклонён",
}


async def _collection_status(db: AsyncSession) -> dict[int, dict]:
    """Per cabinet: last successful collection time and the newest run status.

    {cabinet_id: {"last_ok": datetime | None, "last_status": str | None,
    "last_status_label": str | None}} — from collection_log only.
    """
    last_ok = dict(
        (
            await db.execute(
                select(CollectionLog.cabinet_id, func.max(CollectionLog.finished_at))
                .where(
                    CollectionLog.status == "ok",
                    CollectionLog.cabinet_id.is_not(None),
                )
                .group_by(CollectionLog.cabinet_id)
            )
        ).all()
    )
    rn = (
        func.row_number()
        .over(
            partition_by=CollectionLog.cabinet_id,
            order_by=[CollectionLog.finished_at.desc().nullslast(), CollectionLog.id.desc()],
        )
        .label("rn")
    )
    ranked = select(CollectionLog.cabinet_id, CollectionLog.status, rn).subquery()
    last_status = {
        row.cabinet_id: row.status
        for row in await db.execute(
            select(ranked.c.cabinet_id, ranked.c.status).where(ranked.c.rn == 1)
        )
    }
    result: dict[int, dict] = {}
    for cabinet_id in set(last_ok) | set(last_status):
        status = last_status.get(cabinet_id)
        result[cabinet_id] = {
            "last_ok": last_ok.get(cabinet_id),
            "last_status": status,
            "last_status_label": _RUN_STATUS_LABELS.get(status) if status else None,
        }
    return result


@router.get("/admin/cabinets")
async def admin_cabinets_page(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    added: str | None = None,
    synced: int | None = None,
    replaced: int | None = None,
):
    """Форма добавления кабинета + список подключённых (без токенов).

    ?added=<имя> — подтверждение после добавления; ?synced=1 — после ручного
    «Обновить сейчас»; ?replaced=1 — после замены токена.
    """
    cabinets = (await db.execute(select(Cabinet).order_by(Cabinet.id))).scalars().all()
    collection = await _collection_status(db)
    for cabinet in cabinets:  # template reads st.last_ok on every row
        collection.setdefault(
            cabinet.id,
            {"last_ok": None, "last_status": None, "last_status_label": None},
        )
    return templates.TemplateResponse(
        request,
        "admin_cabinets.html",
        {
            "user": user,
            "cabinets": cabinets,
            "error": None,
            "added": added,
            "synced": synced,
            "replaced": replaced,
            "collection": collection,
        },
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
    """Add a cabinet: token is stored Fernet-encrypted, currency follows wb_type.

    Redirects back with ?added=<имя> so the page can confirm what happened
    (the old silent redirect read as "моргнул и непонятно").
    """
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
    return RedirectResponse(f"/admin/cabinets?added={quote(name)}", status_code=303)


@router.post("/admin/cabinets/{cabinet_id}/sync")
async def admin_cabinet_sync(request: Request, user: _UserDep, db: _DbDep, cabinet_id: int):
    """Ручной сбор по кабинету прямо в запросе; редирект со свежими статусами."""
    if await db.get(Cabinet, cabinet_id) is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    await sync_cabinet_once(cabinet_id)
    return RedirectResponse("/admin/cabinets?synced=1", status_code=303)


@router.post("/admin/cabinets/{cabinet_id}/token")
async def admin_cabinet_token(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    cabinet_id: int,
    token: str = Form(...),
    name: str = Form(""),
):
    """Замена токена (и опционально имени) без пересоздания кабинета.

    Супервизор подхватит новый токен (по отпечатку шифротекста) в течение
    минуты — отдельного рестарта циклов здесь не нужно.
    """
    cabinet = await db.get(Cabinet, cabinet_id)
    if cabinet is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    token = token.strip()
    if not token:
        return await _form_error(db, user, request, "Токен не может быть пустым")
    cabinet.token_encrypted = encrypt_token(token)
    name = name.strip()
    if name:
        cabinet.name = name
    await db.commit()
    return RedirectResponse("/admin/cabinets?replaced=1", status_code=303)


@router.post("/admin/cabinets/{cabinet_id}/delete")
async def admin_cabinet_delete(request: Request, user: _UserDep, db: _DbDep, cabinet_id: int):
    """Удаление кабинета (FK ON DELETE CASCADE чистит все его данные)."""
    cabinet = await db.get(Cabinet, cabinet_id)
    if cabinet is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    await stop_for_cabinet(cabinet_id)  # loops would race the cascade delete otherwise
    await db.delete(cabinet)
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


# --- Task 9: campaign detail, items, health ------------------------------------


_DETAIL_DAYS = (7, 30)


@router.get("/cabinets/{cabinet_id}/campaigns/{advert_id}")
async def campaign_page(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    cabinet_id: int,
    advert_id: int,
    days: int = 7,
):
    """Детальная РК: метрики за период, товары, бюджет/кластеры, графики.

    ?days= принимает только 7 или 30 (иначе 422); отсутствие кабинета или
    РК — 404. Данные графиков едут в страницу как JSON-блок #chart-data
    (Chart.js — enhancement: без JS таблицы и метрики остаются читаемыми).
    """
    cabinet = await db.get(Cabinet, cabinet_id)
    if cabinet is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    if days not in _DETAIL_DAYS:
        raise HTTPException(status_code=422, detail="days must be 7 or 30")
    detail = await campaign_detail(db, cabinet_id, advert_id, days)
    if detail["campaign"] is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    chart_data = {
        "bids": detail["bid_series"],  # [{ts, placement, bid_kopecks}]
        "positions": detail["position_series"],  # [{date, avg}]
        "spend": detail["spend_series"],  # [{date, sum_kopecks|null}]
    }
    return templates.TemplateResponse(
        request,
        "campaign.html",
        {"user": user, "cabinet": cabinet, "detail": detail, "chart_data": chart_data},
    )


@router.get("/items")
async def items_page(request: Request, user: _UserDep, db: _DbDep, cabinet: int | None = None):
    """Сводка по товарам (nm) через все РК кабинета, сортировка по расходу.

    Без ?cabinet= показывается страница выбора кабинета (не ошибка):
    nav-ссылка /items ведёт сюда без параметров.
    """
    cabinets = (
        (await db.execute(select(Cabinet).where(Cabinet.active.is_(True)).order_by(Cabinet.id)))
        .scalars()
        .all()
    )
    if cabinet is None:
        return templates.TemplateResponse(
            request, "items.html", {"user": user, "cabinets": cabinets, "current": None, "rows": []}
        )
    current = await db.get(Cabinet, cabinet)
    if current is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    rows = await items_summary(db, cabinet)
    return templates.TemplateResponse(
        request,
        "items.html",
        {"user": user, "cabinets": cabinets, "current": current, "rows": rows},
    )


# --- Task 4 (phase 2): hours heatmap page --------------------------------------


@router.get("/hours")
async def hours_page(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    cabinet: int | None = None,
    days: int = 7,
):
    """Тепловая карта заказов по часам (МСК) за 7/30 дней.

    Без ?cabinet= показывается страница выбора кабинета (как на /items).
    ?days= принимает только 7 или 30 (иначе 422); неизвестный кабинет — 404.
    Данные графика едут в страницу как JSON-блок #chart-data: ровно 24
    элемента {hour, orders, revenue} (revenue — копейки; Chart.js рисует
    столбцы заказов, выручка — в подсказке). Chart.js — enhancement: без JS
    читается таблица «Час — заказы — выручка».
    """
    if days not in _DETAIL_DAYS:
        raise HTTPException(status_code=422, detail="days must be 7 or 30")
    cabinets = (
        (await db.execute(select(Cabinet).where(Cabinet.active.is_(True)).order_by(Cabinet.id)))
        .scalars()
        .all()
    )
    if cabinet is None:
        return templates.TemplateResponse(
            request,
            "hours.html",
            {"user": user, "cabinets": cabinets, "current": None, "rows": [], "days": days},
        )
    current = await db.get(Cabinet, cabinet)
    if current is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    rows = await hours_heatmap(db, cabinet, days)
    chart_data = [
        {"hour": row["hour"], "orders": row["orders"], "revenue": row["revenue_kopecks"]}
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "hours.html",
        {
            "user": user,
            "cabinets": cabinets,
            "current": current,
            "rows": rows,
            "days": days,
            "chart_data": chart_data,
        },
    )


def _period_label(period_sec: int) -> str:
    """Loop period for the health table: 300 -> "5 мин", 86400 -> "24 ч"."""
    if period_sec >= 3600 and period_sec % 3600 == 0:
        hours = period_sec // 3600
        return f"{hours} ч"
    if period_sec % 60 == 0:
        return f"{period_sec // 60} мин"
    return f"{period_sec} с"


@router.get("/health-page")
async def health_page(request: Request, user: _UserDep, db: _DbDep):
    """Свежесть данных по каждому циклу сбора + журнал (последние 100 строк).

    /health уже занят JSON-проверкой живости (Task 2), поэтому страница
    живёт на /health-page.
    """
    status = await health_status(db)
    cabinet_names = {
        row.id: row.name
        for row in (await db.execute(select(Cabinet))).scalars().all()
    }
    periods = {spec.name: _period_label(spec.period_sec) for spec in LOOPS_SPEC}
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "user": user,
            "loops": status["loops"],
            "log": status["log"],
            "cabinet_names": cabinet_names,
            "periods": periods,
        },
    )
