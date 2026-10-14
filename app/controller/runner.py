"""Контроллер показов (фаза 2): применяет подтверждённые расписания.

Один проход ``controller_tick``:
1. kill-switch (app_settings.controller_kill_switch) -> журнал "skipped"
   (НЕ чаще раза в 10 минут — дедуп по времени) и выход;
2. enabled-расписания кабинета + их кампании (join по advert_id, payment_type
   из campaigns);
3. расход сегодня — из НАШЕЙ БД (stats за сегодня МСК; fullstats-цикл обновляет
   раз в 20 минут) — WB за расходом не дёргаем;
4. желаемое состояние: час now_msk в hours И лимит не превышен. Лимит: cpc —
   нативный daily-limits GET (одна пачка ≤100 cpc-id с заданным лимитом, ответ
   несёт enabled/dailyLimit/spentToday); cpm — нативных лимитов нет (живой
   факт: 400 на CPM), сравниваем наш расход с daily_limit_kopecks;
5. фактическое состояние — campaign.status из БД (9=on, 11=paused, прочие
   кампании контроллер не трогает);
6. расхождение + дедуп (нет ok/dry-строки по (advert_id, action) за последний
   час) -> действие: dry_run — журнал "dry" БЕЗ вызова WB; live —
   client.pause/start, результат "ok"; WbApiError -> журнал "error".
   422 «Статус кампании не изменен» глушится В КЛИЕНТЕ (no-op -> None -> "ok").
7. три подряд error-строки в controller_log кабинета -> return -1
   (цикл-обёртка спит 900с; любая ok/dry-строка сбрасывает серию).

NOTE: daily_limits_put тик НЕ вызывает — нативный лимит согласуется
однократно при применении расписания из UI (Task 6), иначе каждый тик
переписывал бы лимит без изменений.

Настройки (app_settings, jsonb; repos.get_setting/set_setting):
- controller_mode: "dry_run" (по умолчанию) | "live" — глобально на MVP;
- controller_kill_switch: bool — запрет ЛЮБЫХ действий;
- coverage_pct: int (по умолчанию 90) — читается UI-генератором часов
  (Task 6), сам тик его не использует.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Campaign, ControllerLog, Schedule, Stat
from app.repos import get_setting
from app.wb.client import WbClient
from app.wb.errors import WbApiError

logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")  # часы расписаний — МСК (UTC+3, без DST)

Sleep = Callable[[float], Awaitable[None]]

# app_settings keys + defaults (UI Task 6 импортирует те же константы)
MODE_KEY = "controller_mode"
KILL_SWITCH_KEY = "controller_kill_switch"
COVERAGE_PCT_KEY = "coverage_pct"
DEFAULT_MODE = "dry_run"
DEFAULT_KILL_SWITCH = False
DEFAULT_COVERAGE_PCT = 90

MODE_LIVE = "live"

_STATUS_ON = 9  # кампания активна
_STATUS_PAUSED = 11  # кампания на паузе

_MAX_DAILY_LIMIT_IDS = 100  # WB advertIds cap for daily-limits
_DEDUP_WINDOW = timedelta(hours=1)  # ≤1 переключения на (кампания, действие) в час
_KILL_LOG_WINDOW = timedelta(minutes=10)  # skipped-журнал не чаще раза в 10 мин
_MAX_CONSECUTIVE_ERRORS = 3
_ERROR_PAUSE_SEC = 900  # 3 подряд ошибки -> пауза контроллера кабинета 15 мин
_DEFAULT_PERIOD_SEC = 60

_ACTION_CONTROLLER = "controller"  # служебные skipped-строки kill-switch


def _now_msk() -> datetime:
    return datetime.now(_MSK)


def _chunked(items: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def controller_tick(
    client: WbClient,
    session: AsyncSession,
    cabinet_id: int,
    *,
    now_msk: datetime,
) -> int:
    """Один проход контроллера по кабинету; возвращает число действий.

    Действие = запись в controller_log ("ok" в live, "dry" в dry_run, "error"
    при отказе WB). Возвращает -1, когда последние 3 строки журнала кабинета —
    ошибки (обёртка должна спать _ERROR_PAUSE_SEC). Kill-switch возвращает 0.
    """
    kill_switch = await get_setting(session, KILL_SWITCH_KEY, DEFAULT_KILL_SWITCH)
    if kill_switch:
        # skipped-журнал не чаще раза в 10 минут, иначе замусорим controller_log
        cutoff = datetime.now(UTC) - _KILL_LOG_WINDOW
        recent = await session.scalar(
            select(ControllerLog.id).where(
                ControllerLog.cabinet_id == cabinet_id,
                ControllerLog.action == _ACTION_CONTROLLER,
                ControllerLog.status == "skipped",
                ControllerLog.created_at >= cutoff,
            )
        )
        if recent is None:
            await _log(session, cabinet_id, None, _ACTION_CONTROLLER, "skipped", "kill-switch")
        return 0

    schedules = (
        await session.execute(
            select(Schedule, Campaign)
            .join(
                Campaign,
                (Campaign.cabinet_id == Schedule.cabinet_id)
                & (Campaign.advert_id == Schedule.advert_id),
            )
            .where(Schedule.cabinet_id == cabinet_id, Schedule.enabled.is_(True))
            .order_by(Schedule.advert_id)
        )
    ).all()
    if not schedules:
        return 0

    # Расход сегодня по каждой кампании — ОДИН запрос по нашей БД (stats за
    # сегодня МСК обновляются fullstats-циклом раз в 20 минут; WB не дёргаем).
    today = now_msk.date()
    advert_ids = [campaign.advert_id for _schedule, campaign in schedules]
    spend_today = {
        row.advert_id: int(row.spent)
        for row in (
            await session.execute(
                select(
                    Stat.advert_id,
                    func.coalesce(func.sum(Stat.sum_kopecks), 0).label("spent"),
                )
                .where(
                    Stat.cabinet_id == cabinet_id,
                    Stat.date == today,
                    Stat.advert_id.in_(advert_ids),
                )
                .group_by(Stat.advert_id)
            )
        ).all()
    }

    limits = await _cpc_daily_limits(client, session, cabinet_id, schedules)

    mode = await get_setting(session, MODE_KEY, DEFAULT_MODE)
    live = mode == MODE_LIVE
    hour = now_msk.hour
    dedup_cutoff = datetime.now(UTC) - _DEDUP_WINDOW
    actions = 0
    for schedule, campaign in schedules:
        if campaign.status not in (_STATUS_ON, _STATUS_PAUSED):
            continue  # завершённые/удалённые кампании контроллер не трогает
        hours = schedule.hours or []
        hour_on = 0 <= hour < len(hours) and bool(hours[hour])
        limit = schedule.daily_limit_kopecks
        exceeded = False
        if limit is not None:
            if campaign.payment_type == "cpc":
                info = limits.get(campaign.advert_id)
                if info is not None and info["enabled"]:
                    exceeded = info["spentToday"] >= info["dailyLimit"]
            else:
                # CPM (+ прочие не-cpc): нативных дневных лимитов нет (живой
                # факт: 400 на CPM) — контроль по нашему расходу за сегодня
                exceeded = spend_today.get(campaign.advert_id, 0) >= limit
        desired_on = hour_on and not exceeded
        if desired_on and campaign.status == _STATUS_PAUSED:
            action = "start"
        elif not desired_on and campaign.status == _STATUS_ON:
            action = "pause"
        else:
            continue  # фактическое состояние совпадает с желаемым

        if await _acted_recently(session, cabinet_id, campaign.advert_id, action, dedup_cutoff):
            continue  # дедуп: ≤1 переключения на (кампания, действие) в час

        detail = _detail(hour, hour_on, exceeded)
        if live:
            try:
                if action == "start":
                    await client.start(campaign.advert_id)
                else:
                    await client.pause(campaign.advert_id)
            except WbApiError as exc:
                await _log(session, cabinet_id, campaign.advert_id, action, "error", str(exc))
                actions += 1
                continue
            await _log(session, cabinet_id, campaign.advert_id, action, "ok", detail)
        else:
            await _log(
                session,
                cabinet_id,
                campaign.advert_id,
                action,
                "dry",
                f"would {action}: {detail}",
            )
        actions += 1

    if await _three_consecutive_errors(session, cabinet_id):
        return -1
    return actions


async def controller_forever(
    client_factory: Callable[[], WbClient],
    *,
    cabinet_id: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    period_sec: int = _DEFAULT_PERIOD_SEC,
) -> None:
    """controller_tick каждые period_sec для ОДНОГО кабинета, пока не отменят.

    Запускается супервизором (app.collector.runner.start_for_cabinet) по
    задаче на активный кабинет: client_factory() отдаёт общий WbClient
    кабинета (при смене токена супервизор перезапускает кабинет — контроллер
    получает новый клиент). -1 (3 подряд ошибки) -> дополнительный сон 900с.
    Отмена — asyncio.cancel (KeyboardInterrupt-безопасность не нужна).
    """
    while True:
        result = 0
        try:
            async with SessionLocal() as session:
                result = await controller_tick(
                    client_factory(), session, cabinet_id, now_msk=_now_msk()
                )
        except Exception:
            logger.exception("controller tick failed (cabinet %s)", cabinet_id)
        if result == -1:
            logger.error(
                "controller (cabinet %d): %d consecutive errors, pausing %ds",
                cabinet_id,
                _MAX_CONSECUTIVE_ERRORS,
                _ERROR_PAUSE_SEC,
            )
            await sleep(_ERROR_PAUSE_SEC)
        await sleep(period_sec)


# -- internals ---------------------------------------------------------------


async def _cpc_daily_limits(
    client: WbClient,
    session: AsyncSession,
    cabinet_id: int,
    schedules: list[tuple[Schedule, Campaign]],
) -> dict[int, dict]:
    """Нативные дневные лимиты для cpc-кампаний с заданным лимитом.

    Одна пачка ≤100 id (обычно один запрос на кабинет). Ошибка чтения не
    валит тик: пишется "error"-строка (учитывается серией ошибок), лимиты
    считаются неизвестными — cpc-кампании оцениваются только по часам.
    """
    ids = [
        campaign.advert_id
        for schedule, campaign in schedules
        if schedule.daily_limit_kopecks is not None and campaign.payment_type == "cpc"
    ]
    info: dict[int, dict] = {}
    if not ids:
        return info
    try:
        for chunk in _chunked(ids, _MAX_DAILY_LIMIT_IDS):
            for item in await client.daily_limits_get(chunk):
                info[int(item["advertId"])] = item
    except WbApiError as exc:
        logger.warning("controller (cabinet %s): daily_limits_get failed: %s", cabinet_id, exc)
        await _log(session, cabinet_id, None, "limit", "error", str(exc))
        return {}
    return info


async def _acted_recently(
    session: AsyncSession,
    cabinet_id: int,
    advert_id: int,
    action: str,
    since: datetime,
) -> bool:
    """Было ли уже ok/dry-действие (advert_id, action) за последний час (EXISTS)."""
    recent = await session.scalar(
        select(ControllerLog.id).where(
            ControllerLog.cabinet_id == cabinet_id,
            ControllerLog.advert_id == advert_id,
            ControllerLog.action == action,
            ControllerLog.status.in_(("ok", "dry")),
            ControllerLog.created_at >= since,
        )
    )
    return recent is not None


async def _three_consecutive_errors(session: AsyncSession, cabinet_id: int) -> bool:
    """Последние 3 строки журнала кабинета — все "error" (любая другая строка
    — ok/dry/skipped — сбрасывает серию, как IntervalLoop после паузы)."""
    statuses = (
        await session.scalars(
            select(ControllerLog.status)
            .where(ControllerLog.cabinet_id == cabinet_id)
            .order_by(ControllerLog.id.desc())
            .limit(_MAX_CONSECUTIVE_ERRORS)
        )
    ).all()
    return len(statuses) == _MAX_CONSECUTIVE_ERRORS and set(statuses) == {"error"}


def _detail(hour: int, hour_on: bool, exceeded: bool) -> str:
    parts = [f"hour={hour} schedule={'on' if hour_on else 'off'}"]
    if exceeded:
        parts.append("daily limit reached")
    return ", ".join(parts)


async def _log(
    session: AsyncSession,
    cabinet_id: int | None,
    advert_id: int | None,
    action: str,
    status: str,
    detail: str | None,
) -> None:
    """Одна строка controller_log (свой commit — как repos.*)."""
    await session.execute(
        insert(ControllerLog).values(
            cabinet_id=cabinet_id,
            advert_id=advert_id,
            action=action,
            status=status,
            detail=detail,
        )
    )
    await session.commit()
