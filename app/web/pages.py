"""Dashboard pages: overview, one cabinet's campaigns, admin cabinets (Task 8)
plus campaign detail, items summary and health (Task 9).

All routes are auth-protected via require_user. Money/type/status/freshness
rendering goes through app/web/format.py, registered as Jinja globals so the
templates stay logic-free. Tokens are only ever encrypted (encrypt_token) and
never rendered back.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.runner import LOOPS_SPEC, stop_for_cabinet, sync_cabinet_once
from app.controller.generator import generate_hours
from app.controller.runner import (
    COVERAGE_PCT_KEY,
    DEFAULT_COVERAGE_PCT,
    DEFAULT_KILL_SWITCH,
    DEFAULT_MODE,
    KILL_SWITCH_KEY,
    MODE_KEY,
    MODE_LIVE,
)
from app.crypto import decrypt_token, encrypt_token
from app.models import Cabinet, Campaign, CollectionLog, ControllerLog, Schedule, User
from app.queries import (
    STATUS_FILTERS,
    cabinet_campaigns,
    campaign_detail,
    health_status,
    hours_heatmap,
    items_summary,
    overview,
)
from app.repos import get_setting, set_setting
from app.wb.client import WbClient
from app.wb.errors import WbApiError
from app.wb.limiter import LimiterRegistry
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


# --- Task 6 (phase 2): schedules page -------------------------------------------
#
# Расписания показов: баннер режима (dry_run/live) + kill-switch, таблица РК
# с чекбоксами часов и дневным лимитом, «Применить», «из данных» (генератор
# часов из тепловой карты) и журнал контроллера (последние 100).
#
# ПРЕДОХРАНИТЕЛЬ (spec-phase2, Global Constraints): write-вызов WB
# (daily_limits_put) выполняется ТОЛЬКО при controller_mode="live"; в
# dry_run роут лишь пишет controller_log со status="dry".


def _hours_mask(hours: list[int]) -> list[int]:
    """Отсортированный список часов генератора -> маска 24x0/1."""
    on = set(hours)
    return [1 if hour in on else 0 for hour in range(24)]


def _parse_hours(form) -> list[int] | None:
    """Часы из формы: скрытый input `hours` с JSON-массивом ИЛИ чекбоксы
    hour_0..hour_23. None — некорректный вход (не JSON, не 24 элемента,
    значения не 0/1) -> роут вернёт 422."""
    raw = str(form.get("hours", "")).strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if (
            not isinstance(data, list)
            or len(data) != 24
            or not all(item in (0, 1) for item in data)
        ):
            return None
        return data
    return [1 if form.get(f"hour_{hour}") else 0 for hour in range(24)]


def _parse_limit(raw: str) -> int | None | str:
    """Дневной лимит из формы (в единицах валюты) -> копейки.

    None — пустое поле (лимита нет); int — round(float * 100);
    str — сообщение об ошибке (не число, отрицательное, бесконечность).
    """
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    try:
        kopecks = round(float(raw) * 100)
    except (ValueError, OverflowError):  # "abc", nan, inf
        return "Дневной лимит: ожидается число в единицах валюты"
    if kopecks < 0:
        return "Дневной лимит не может быть отрицательным"
    return kopecks


def _limit_input_value(kopecks: int | None) -> str:
    """Значение поля лимита: money-формат в единицах валюты ("1500.00")."""
    return f"{kopecks / 100:.2f}" if kopecks is not None else ""


async def _last_actions(db: AsyncSession, cabinet_id: int) -> dict[int, ControllerLog]:
    """Последняя строка controller_log по каждой кампании кабинета."""
    rn = (
        func.row_number()
        .over(
            partition_by=ControllerLog.advert_id,
            order_by=[ControllerLog.created_at.desc(), ControllerLog.id.desc()],
        )
        .label("rn")
    )
    ranked = select(
        ControllerLog.advert_id,
        ControllerLog.action,
        ControllerLog.status,
        ControllerLog.created_at,
        rn,
    ).subquery()
    return {
        row.advert_id: row
        for row in await db.execute(
            select(
                ranked.c.advert_id,
                ranked.c.action,
                ranked.c.status,
                ranked.c.created_at,
            ).where(ranked.c.rn == 1)
        )
    }


async def _schedules_context(db: AsyncSession, cabinet: Cabinet) -> dict:
    """Данные страницы расписаний: настройки режима, РК + расписания, журнал."""
    mode = await get_setting(db, MODE_KEY, DEFAULT_MODE)
    kill_switch = await get_setting(db, KILL_SWITCH_KEY, DEFAULT_KILL_SWITCH)
    campaigns = await cabinet_campaigns(db, cabinet.id)
    schedules = {
        schedule.advert_id: schedule
        for schedule in (
            await db.execute(select(Schedule).where(Schedule.cabinet_id == cabinet.id))
        )
        .scalars()
        .all()
    }
    last_actions = await _last_actions(db, cabinet.id)
    rows = []
    for campaign in campaigns:
        schedule = schedules.get(campaign["advert_id"])
        rows.append(
            {
                "advert_id": campaign["advert_id"],
                "name": campaign["name"],
                "payment_type": campaign["payment_type"],
                "status": campaign["status"],
                "hours": schedule.hours if schedule is not None else [0] * 24,
                "enabled": schedule.enabled if schedule is not None else False,
                "limit_value": _limit_input_value(
                    schedule.daily_limit_kopecks if schedule is not None else None
                ),
                "last_action": last_actions.get(campaign["advert_id"]),
            }
        )
    log = (
        (
            await db.execute(
                select(ControllerLog).order_by(ControllerLog.id.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    cabinet_names = {
        row.id: row.name for row in (await db.execute(select(Cabinet))).scalars().all()
    }
    return {
        "mode": mode,
        "kill_switch": kill_switch,
        "rows": rows,
        "log": list(log),
        "cabinet_names": cabinet_names,
    }


async def _render_schedules(
    request: Request,
    user: User,
    db: AsyncSession,
    cabinet: Cabinet,
    *,
    error: str | None = None,
    applied: str | None = None,
    generated: int | None = None,
    status_code: int = 200,
):
    """Render the schedules page (shared by GET and the 422 error paths)."""
    context = await _schedules_context(db, cabinet)
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "user": user,
            "current": cabinet,
            "error": error,
            "applied": applied,
            "generated": generated,
            **context,
        },
        status_code=status_code,
    )


async def _controller_log(
    db: AsyncSession,
    cabinet_id: int,
    advert_id: int | None,
    action: str,
    status: str,
    detail: str | None,
) -> None:
    """Одна строка controller_log (свой commit — как controller.runner._log)."""
    db.add(
        ControllerLog(
            cabinet_id=cabinet_id,
            advert_id=advert_id,
            action=action,
            status=status,
            detail=detail,
        )
    )
    await db.commit()


@router.get("/schedules")
async def schedules_page(
    request: Request,
    user: _UserDep,
    db: _DbDep,
    cabinet: int | None = None,
    applied: str | None = None,
    generated: int | None = None,
):
    """Расписания показов: баннер режима, kill-switch, РК с часами и лимитами.

    Без ?cabinet= показывается страница выбора кабинета (как на /hours);
    неизвестный кабинет — 404.
    """
    cabinets = (
        (await db.execute(select(Cabinet).where(Cabinet.active.is_(True)).order_by(Cabinet.id)))
        .scalars()
        .all()
    )
    if cabinet is None:
        return templates.TemplateResponse(
            request,
            "schedules.html",
            {"user": user, "cabinets": cabinets, "current": None},
        )
    current = await db.get(Cabinet, cabinet)
    if current is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    return await _render_schedules(
        request, user, db, current, applied=applied, generated=generated
    )


@router.post("/schedules/generate")
async def schedules_generate(request: Request, user: _UserDep, db: _DbDep, cabinet: int):
    """«Из данных»: пересчитать часы по generate_hours(hours_heatmap(30), coverage_pct).

    Расписания создаются/обновляются для ВСЕХ кампаний кабинета с
    enabled=false — генерация только пре-заполняет часы, включение происходит
    кнопкой «Применить». daily_limit_kopecks/applied_at существующих строк не
    трогаются.
    """
    current = await db.get(Cabinet, cabinet)
    if current is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    heatmap = await hours_heatmap(db, cabinet, 30)
    coverage = int(await get_setting(db, COVERAGE_PCT_KEY, DEFAULT_COVERAGE_PCT))
    mask = _hours_mask(generate_hours(heatmap, coverage_pct=coverage))
    advert_ids = (
        (
            await db.execute(
                select(Campaign.advert_id).where(Campaign.cabinet_id == cabinet)
            )
        )
        .scalars()
        .all()
    )
    generated_count = 0
    if advert_ids:
        values = [
            {
                "cabinet_id": cabinet,
                "advert_id": advert_id,
                "hours": mask,
                "enabled": False,
                "updated_at": func.now(),
            }
            for advert_id in advert_ids
        ]
        stmt = pg_insert(Schedule).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["cabinet_id", "advert_id"],
            set_={
                "hours": stmt.excluded.hours,
                "enabled": stmt.excluded.enabled,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        result = await db.execute(stmt)
        await db.commit()
        generated_count = int(result.rowcount)
    return RedirectResponse(
        f"/schedules?cabinet={cabinet}&generated={generated_count}", status_code=303
    )


@router.post("/schedules/killswitch")
async def schedules_killswitch(
    request: Request, user: _UserDep, db: _DbDep, cabinet: int | None = Form(None)
):
    """Кнопка «Стоп всё»/«Возобновить»: toggle controller_kill_switch."""
    current = await get_setting(db, KILL_SWITCH_KEY, DEFAULT_KILL_SWITCH)
    await set_setting(db, KILL_SWITCH_KEY, not current)
    target = f"/schedules?cabinet={cabinet}" if cabinet is not None else "/schedules"
    return RedirectResponse(target, status_code=303)


@router.post("/schedules/mode")
async def schedules_mode(
    request: Request, user: _UserDep, db: _DbDep, cabinet: int | None = Form(None)
):
    """Toggle controller_mode dry_run <-> live.

    На живом сервере перевод в live защищён onsubmit-подтверждением в UI;
    отдельной парольной защиты нет — kill-switch и dry_run по умолчанию
    достаточны как предохранители на MVP (решение владельца).
    """
    mode = await get_setting(db, MODE_KEY, DEFAULT_MODE)
    await set_setting(db, MODE_KEY, DEFAULT_MODE if mode == MODE_LIVE else MODE_LIVE)
    target = f"/schedules?cabinet={cabinet}" if cabinet is not None else "/schedules"
    return RedirectResponse(target, status_code=303)


@router.post("/schedules/{cabinet_id}/{advert_id}")
async def schedules_apply(
    request: Request, user: _UserDep, db: _DbDep, cabinet_id: int, advert_id: int
):
    """«Применить»: upsert расписания (часы, дневной лимит, enabled).

    daily_limit приходит в единицах валюты строкой -> round(x*100) копеек;
    некорректный вход («abc», «-5», плохой JSON часов) — 422 с перерисовкой
    страницы и сообщением. Для cpc с лимитом в live дополнительно ставится
    нативный дневной лимит WB (daily_limits_put, carry_over); в dry_run —
    только controller_log(action="limit", status="dry") без write-вызова.
    """
    cabinet = await db.get(Cabinet, cabinet_id)
    if cabinet is None:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    campaign = await db.get(Campaign, (cabinet_id, advert_id))
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    form = await request.form()

    hours = _parse_hours(form)
    if hours is None:
        return await _render_schedules(
            request,
            user,
            db,
            cabinet,
            error="Часы: ожидается JSON-массив из 24 значений 0/1 (или чекбоксы)",
            status_code=422,
        )
    limit_kopecks = _parse_limit(str(form.get("daily_limit", "")))
    if isinstance(limit_kopecks, str):
        return await _render_schedules(
            request, user, db, cabinet, error=limit_kopecks, status_code=422
        )
    enabled = form.get("enabled") is not None

    stmt = pg_insert(Schedule).values(
        cabinet_id=cabinet_id,
        advert_id=advert_id,
        hours=hours,
        daily_limit_kopecks=limit_kopecks,
        enabled=enabled,
        updated_at=func.now(),
        applied_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["cabinet_id", "advert_id"],
        set_={
            "hours": stmt.excluded.hours,
            "daily_limit_kopecks": stmt.excluded.daily_limit_kopecks,
            "enabled": stmt.excluded.enabled,
            "updated_at": stmt.excluded.updated_at,
            "applied_at": stmt.excluded.applied_at,
        },
    )
    await db.execute(stmt)
    await db.commit()

    # Нативный дневной лимит (только cpc): write в WB — ИСКЛЮЧИТЕЛЬНО в live.
    if enabled and limit_kopecks is not None and campaign.payment_type == "cpc":
        mode = await get_setting(db, MODE_KEY, DEFAULT_MODE)
        if mode == MODE_LIVE:
            client = WbClient(
                decrypt_token(cabinet.token_encrypted),
                LimiterRegistry(),
                cabinet_id=cabinet_id,
            )
            try:
                await client.daily_limits_put(
                    [advert_id],
                    enabled=True,
                    daily_limit_kopecks=limit_kopecks,
                    carry_over=True,
                )
            except WbApiError as exc:
                await _controller_log(db, cabinet_id, advert_id, "limit", "error", str(exc))
            else:
                await _controller_log(
                    db,
                    cabinet_id,
                    advert_id,
                    "limit",
                    "ok",
                    f"native daily limit {limit_kopecks} kopecks (carry over)",
                )
            finally:
                await client.aclose()
        else:
            await _controller_log(
                db,
                cabinet_id,
                advert_id,
                "limit",
                "dry",
                f"would set native daily limit {limit_kopecks} kopecks",
            )

    return RedirectResponse(
        f"/schedules?cabinet={cabinet_id}&applied={quote(campaign.name)}",
        status_code=303,
    )
