"""Контроллер: fake client + fake MSK-время + реальный dev-Postgres.

Проверяются предохранители spec-phase2: kill-switch, dry_run (нет вызовов
API, есть журнал "dry"), дедуп (≤1 переключения на (кампания, действие) в
час), CPM-стоп по нашему расходу, cpc-лимит из daily-limits, старт после
полуночи, 3 подряд ошибки -> -1, 422 no-op не считается ошибкой. Сессии —
db_session fixture (per-test savepoint, всё откатывается).
"""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller import runner as controller
from app.controller.runner import (
    KILL_SWITCH_KEY,
    MODE_KEY,
    controller_forever,
    controller_tick,
)
from app.models import ControllerLog, Schedule
from app.repos import set_setting
from app.wb.errors import WbApiError
from tests.helpers import make_cabinet, make_campaign, make_stat

_MSK = timezone(timedelta(hours=3))
_TODAY = date(2026, 10, 14)
# DB-backed tests must share the session-scoped loop of the db_session fixture.
_db = pytest.mark.asyncio(loop_scope="session")


def _msk(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 14, hour, minute, tzinfo=_MSK)


def _hours_on(*on_hours: int) -> list[int]:
    """24×0/1 mask with 1 at the given hours."""
    return [1 if hour in on_hours else 0 for hour in range(24)]


DAY_ON = _hours_on(*range(8, 24))  # активны дневные часы 8..23
ALL_ON = _hours_on(*range(24))


class FakeControllerClient:
    """WbClient stand-in: records pause/start/daily_limits_get calls.

    pause/start return None (как боевой клиент: 200 без тела И 422 «Статус
    кампании не изменен» — no-op); ошибки инжектируются через fail_*.
    """

    def __init__(
        self,
        *,
        limits: list[dict] | None = None,
        fail_pause: WbApiError | None = None,
        fail_start: WbApiError | None = None,
    ) -> None:
        self.pause_calls: list[int] = []
        self.start_calls: list[int] = []
        self.limits_calls: list[list[int]] = []
        self._limits = limits or []
        self._fail_pause = fail_pause
        self._fail_start = fail_start

    async def pause(self, advert_id: int) -> None:
        if self._fail_pause is not None:
            raise self._fail_pause
        self.pause_calls.append(advert_id)

    async def start(self, advert_id: int) -> None:
        if self._fail_start is not None:
            raise self._fail_start
        self.start_calls.append(advert_id)

    async def daily_limits_get(self, advert_ids: list[int]) -> list[dict]:
        self.limits_calls.append(list(advert_ids))
        return [dict(item) for item in self._limits if item["advertId"] in advert_ids]


async def _seed_campaign(
    db_session: AsyncSession,
    *,
    advert_id: int = 1001,
    payment_type: str = "cpm",
    status: int = 9,
    hours: list[int] | None = None,
    limit: int | None = None,
    enabled: bool = True,
) -> int:
    """Cabinet + campaign + enabled schedule; returns the cabinet id."""
    cabinet = make_cabinet(name="ctrl-test")
    db_session.add(cabinet)
    await db_session.flush()
    db_session.add(make_campaign(cabinet.id, advert_id, payment_type=payment_type, status=status))
    db_session.add(
        Schedule(
            cabinet_id=cabinet.id,
            advert_id=advert_id,
            hours=hours if hours is not None else DAY_ON,
            daily_limit_kopecks=limit,
            enabled=enabled,
        )
    )
    await db_session.commit()
    return cabinet.id


async def _logs(db_session: AsyncSession) -> list[ControllerLog]:
    return list(
        (await db_session.execute(select(ControllerLog).order_by(ControllerLog.id))).scalars().all()
    )


# --- kill-switch ---------------------------------------------------------------


@_db
async def test_kill_switch_skips_without_calls(db_session: AsyncSession):
    cab = await _seed_campaign(db_session)
    await set_setting(db_session, KILL_SWITCH_KEY, True)
    client = FakeControllerClient()

    assert await controller_tick(client, db_session, cab, now_msk=_msk(12)) == 0

    assert client.pause_calls == [] and client.start_calls == []
    (log,) = await _logs(db_session)
    assert (log.cabinet_id, log.advert_id, log.action, log.status, log.detail) == (
        cab,
        None,
        "controller",
        "skipped",
        "kill-switch",
    )


@_db
async def test_kill_switch_skipped_log_deduped_within_10_minutes(db_session: AsyncSession):
    cab = await _seed_campaign(db_session)
    await set_setting(db_session, KILL_SWITCH_KEY, True)
    client = FakeControllerClient()

    await controller_tick(client, db_session, cab, now_msk=_msk(12))
    await controller_tick(client, db_session, cab, now_msk=_msk(12, 5))

    assert len([log for log in await _logs(db_session) if log.status == "skipped"]) == 1


# --- dry_run / live переключения -------------------------------------------------


@_db
async def test_dry_run_logs_dry_without_api_calls(db_session: AsyncSession):
    """Режим по умолчанию (ключа нет) = dry_run: ноль вызовов WB, журнал "dry"."""
    cab = await _seed_campaign(db_session, status=9)  # активна, час 3 — вне расписания
    client = FakeControllerClient()

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(3))

    assert actions == 1
    assert client.pause_calls == [] and client.start_calls == []
    (log,) = await _logs(db_session)
    assert (log.action, log.status, log.advert_id) == ("pause", "dry", 1001)
    assert log.detail.startswith("would pause")


@_db
async def test_dry_run_would_start_paused_in_active_hour(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=11, hours=ALL_ON)
    client = FakeControllerClient()

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(12))

    assert actions == 1
    assert client.start_calls == []  # dry_run — никаких вызовов
    (log,) = await _logs(db_session)
    assert (log.action, log.status) == ("start", "dry")


@_db
async def test_live_pauses_active_campaign_outside_hours(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9)
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient()

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(3))

    assert actions == 1
    assert client.pause_calls == [1001] and client.start_calls == []
    (log,) = await _logs(db_session)
    assert (log.action, log.status, log.advert_id) == ("pause", "ok", 1001)
    assert "hour=3 schedule=off" in log.detail


@_db
async def test_live_starts_paused_campaign_in_active_hour(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=11, hours=ALL_ON)
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient()

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(12))

    assert actions == 1
    assert client.start_calls == [1001]
    (log,) = await _logs(db_session)
    assert (log.action, log.status) == ("start", "ok")


@_db
async def test_matching_state_and_foreign_statuses_are_untouched(db_session: AsyncSession):
    """Совпадение желаемого/фактического и статусы не 9/11 — действий нет."""
    cab = await _seed_campaign(db_session, status=9)
    client = FakeControllerClient()

    assert await controller_tick(client, db_session, cab, now_msk=_msk(12)) == 0  # день: и так on

    db_session.add(make_campaign(cab, 1002, payment_type="cpm", status=7))  # завершена
    db_session.add(Schedule(cabinet_id=cab, advert_id=1002, hours=ALL_ON, enabled=True))
    await db_session.commit()
    assert await controller_tick(client, db_session, cab, now_msk=_msk(12)) == 0
    assert await _logs(db_session) == []


@_db
async def test_disabled_schedule_is_ignored(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9, enabled=False)
    client = FakeControllerClient()

    assert await controller_tick(client, db_session, cab, now_msk=_msk(3)) == 0
    assert await _logs(db_session) == []


# --- дедуп -----------------------------------------------------------------------


@_db
async def test_second_tick_same_hour_does_not_duplicate(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9)
    client = FakeControllerClient()

    assert await controller_tick(client, db_session, cab, now_msk=_msk(3)) == 1
    assert await controller_tick(client, db_session, cab, now_msk=_msk(3, 30)) == 0

    logs = await _logs(db_session)
    assert len(logs) == 1  # ok/dry за час по (advert_id, action) — второй нет


# --- CPM: лимит по нашему расходу -------------------------------------------------


@_db
async def test_cpm_limit_reached_pauses_even_in_active_hour(db_session: AsyncSession):
    """CPM: расход сегодня ≥ лимита -> пауза, даже если час активный."""
    cab = await _seed_campaign(db_session, status=9, payment_type="cpm", limit=100000)
    db_session.add_all(
        [
            make_stat(cab, 1001, nm_id=1, date=_TODAY, sum_kopecks=60000),
            make_stat(cab, 1001, nm_id=2, date=_TODAY, sum_kopecks=40000),  # суммарно 100000
        ]
    )
    await db_session.commit()
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient()

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(12))

    assert actions == 1
    assert client.pause_calls == [1001]
    (log,) = await _logs(db_session)
    assert log.status == "ok" and "daily limit reached" in log.detail


@_db
async def test_cpm_below_limit_keeps_running_in_active_hour(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9, payment_type="cpm", limit=100000)
    db_session.add(make_stat(cab, 1001, nm_id=1, date=_TODAY, sum_kopecks=99999))
    await db_session.commit()
    client = FakeControllerClient()

    assert await controller_tick(client, db_session, cab, now_msk=_msk(12)) == 0
    assert client.pause_calls == []


@_db
async def test_cpm_midnight_resets_spend_and_starts(db_session: AsyncSession):
    """После 00:00 МСК расход today=0 -> превышения нет -> старт (час активен)."""
    cab = await _seed_campaign(
        db_session, status=11, payment_type="cpm", limit=100000, hours=ALL_ON
    )
    db_session.add(
        make_stat(cab, 1001, nm_id=1, date=_TODAY - timedelta(days=1), sum_kopecks=500000)
    )
    await db_session.commit()
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient()

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(0, 5))

    assert actions == 1
    assert client.start_calls == [1001]


# --- CPC: лимит из нативного daily-limits ------------------------------------------


def _limit(advert_id: int, *, enabled: bool, daily: int, spent: int) -> dict:
    return {
        "advertId": advert_id,
        "enabled": enabled,
        "dailyLimit": daily,
        "spentToday": spent,
        "carryOverEnabled": False,
        "valid": True,
        "requiredLimit": 100000,
    }


@_db
async def test_cpc_native_limit_reached_pauses(db_session: AsyncSession):
    """CPC: spentToday ≥ dailyLimit из daily-limits -> пауза в активный час."""
    cab = await _seed_campaign(db_session, status=9, payment_type="cpc", limit=50000)
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient(limits=[_limit(1001, enabled=True, daily=50000, spent=60000)])

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(12))

    assert actions == 1
    assert client.limits_calls == [[1001]]  # одна пачка только cpc-шных с лимитом
    assert client.pause_calls == [1001]


@_db
async def test_cpc_below_native_limit_and_cpm_ids_not_queried(db_session: AsyncSession):
    """CPC ниже лимита — без действий; cpm-кампания с лимитом не попадает в пачку."""
    cab = await _seed_campaign(db_session, status=9, payment_type="cpc", limit=50000)
    db_session.add(make_campaign(cab, 1002, payment_type="cpm", status=9))
    db_session.add(Schedule(cabinet_id=cab, advert_id=1002, hours=ALL_ON, daily_limit_kopecks=1))
    await db_session.commit()
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient(limits=[_limit(1001, enabled=True, daily=50000, spent=100)])

    assert await controller_tick(client, db_session, cab, now_msk=_msk(12)) == 0

    assert client.limits_calls == [[1001]]  # только cpc; cpm контролируется нашей stats
    assert client.pause_calls == []


# --- ошибки: серия из 3 -> -1; 422 не ошибка -----------------------------------------


@_db
async def test_three_consecutive_errors_pause_controller(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9)
    db_session.add_all(
        [make_campaign(cab, advert_id, payment_type="cpm", status=9) for advert_id in (1002, 1003)]
    )
    db_session.add_all(
        [
            Schedule(cabinet_id=cab, advert_id=aid, hours=DAY_ON, enabled=True)
            for aid in (1002, 1003)
        ]
    )
    await db_session.commit()
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient(fail_pause=WbApiError(500, "wb down"))

    assert await controller_tick(client, db_session, cab, now_msk=_msk(3)) == -1

    logs = await _logs(db_session)
    assert len(logs) == 3
    assert all(log.status == "error" and "wb down" in log.detail for log in logs)


@_db
async def test_two_errors_are_not_a_pause_yet(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9)
    db_session.add(make_campaign(cab, 1002, payment_type="cpm", status=9))
    db_session.add(Schedule(cabinet_id=cab, advert_id=1002, hours=DAY_ON, enabled=True))
    await db_session.commit()
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient(fail_pause=WbApiError(500, "wb down"))

    assert await controller_tick(client, db_session, cab, now_msk=_msk(3)) == 2
    # серия не сброшена: следующий тик добивает третью ошибку -> -1
    assert await controller_tick(client, db_session, cab, now_msk=_msk(4)) == -1


@_db
async def test_error_series_resets_after_ok(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9)
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient(fail_pause=WbApiError(500, "wb down"))

    # 3 кампании падают в одном тике -> -1 (3 error-строки)
    for advert_id in (1002, 1003):
        db_session.add(make_campaign(cab, advert_id, payment_type="cpm", status=9))
        db_session.add(Schedule(cabinet_id=cab, advert_id=advert_id, hours=DAY_ON, enabled=True))
    await db_session.commit()
    assert await controller_tick(client, db_session, cab, now_msk=_msk(3)) == -1

    # WB починился: ошибки не дедупируются -> действия повторяются и пишут "ok"
    client._fail_pause = None
    actions = await controller_tick(client, db_session, cab, now_msk=_msk(4))
    assert actions == 3 and len(client.pause_calls) == 3
    assert await controller_tick(client, db_session, cab, now_msk=_msk(5)) != -1


@_db
async def test_422_noop_is_logged_ok_not_error(db_session: AsyncSession):
    """Боевой клиент возвращает None и на 200, и на 422 «Статус не изменен»:
    для контроллера это обычное успешное действие (respx-тест 422 — в tests/wb)."""
    cab = await _seed_campaign(db_session, status=11, hours=ALL_ON)
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient()  # start() -> None (no-op 422 симулирован)

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(12))

    assert actions == 1
    (log,) = await _logs(db_session)
    assert log.status == "ok"


# --- daily_limits_get упал — тик выживает ---------------------------------------------


@_db
async def test_daily_limits_get_error_is_logged_tick_continues(db_session: AsyncSession):
    cab = await _seed_campaign(db_session, status=9, payment_type="cpc", limit=50000)
    await set_setting(db_session, MODE_KEY, "live")
    client = FakeControllerClient()  # limits пуст: WB не ответил
    client.daily_limits_get = _boom  # type: ignore[method-assign]

    actions = await controller_tick(client, db_session, cab, now_msk=_msk(12))

    # лимиты неизвестны -> решение по часам (час активный, кампания on) -> 0 действий
    assert actions == 0
    (log,) = await _logs(db_session)
    assert (log.action, log.status, log.advert_id) == ("limit", "error", None)


async def _boom(advert_ids: list[int]) -> list[dict]:
    raise WbApiError(500, "wb down")


# --- controller_forever ----------------------------------------------------------------


@_db
async def test_controller_forever_ticks_then_sleeps(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    results = [-1, 0]

    async def fake_tick(client, session, cabinet_id, *, now_msk):
        return results.pop(0)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError  # stop after: 900 (ошибки) + 2 периода

    monkeypatch.setattr(controller, "controller_tick", fake_tick)
    monkeypatch.setattr(controller, "SessionLocal", _session_factory(db_session))
    with pytest.raises(asyncio.CancelledError):
        await controller_forever(
            FakeControllerClient, cabinet_id=1, sleep=fake_sleep, period_sec=60
        )

    assert sleeps == [900, 60, 60]  # после -1 спит 900с, иначе — период


def _session_factory(session: AsyncSession):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield session

    return factory
