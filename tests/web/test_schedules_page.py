"""Task 6 (phase 2) page tests: /schedules — расписания, kill-switch, режим.

Same DB strategy as tests/web/test_hours_page.py: seeding and verification
run against the app database (app.db.SessionLocal) through a throwaway
engine; the seeded cabinet is deleted afterwards (FK ON DELETE CASCADE
cleans campaigns/schedules/orders/controller_log too).

app_settings (controller_mode, controller_kill_switch, coverage_pct) are
GLOBAL rows not tied to a cabinet, so an autouse fixture resets them to the
dry_run defaults before and after every test.

WbClient is monkeypatched at app.web.pages (like runner.WbClient in
test_cabinet_admin): the fake counts daily_limits_put calls, so the dry_run
vs live behaviour of POST apply is verified without network.
"""

import asyncio
import json
import os
import re
from urllib.parse import quote

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.controller.runner import (
    COVERAGE_PCT_KEY,
    DEFAULT_COVERAGE_PCT,
    DEFAULT_KILL_SWITCH,
    DEFAULT_MODE,
    KILL_SWITCH_KEY,
    MODE_KEY,
)
from app.models import Cabinet, ControllerLog, Schedule
from app.queries import msk_today
from app.repos import get_setting, set_setting
from app.web import pages
from tests.helpers import make_cabinet, make_campaign, make_order


def _run_db(coro) -> object:
    """Run a `async def coro(db)` against a throwaway engine, return its result."""

    async def _with_engine():
        engine = create_async_engine(os.environ["BIDER_DATABASE_URL"])
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await coro(db)
        finally:
            await engine.dispose()

    return asyncio.run(_with_engine())


@pytest.fixture(autouse=True)
def controller_settings():
    """Reset the global controller settings around every test (dry_run, off)."""

    async def _reset(db):
        await set_setting(db, MODE_KEY, DEFAULT_MODE)
        await set_setting(db, KILL_SWITCH_KEY, DEFAULT_KILL_SWITCH)
        await set_setting(db, COVERAGE_PCT_KEY, DEFAULT_COVERAGE_PCT)

    _run_db(_reset)
    yield
    _run_db(_reset)


@pytest.fixture
def cabinet_ids() -> list[int]:
    """Deletes the cabinets a test created (FK CASCADE cleans children)."""
    ids: list[int] = []

    async def _cleanup(db):
        if ids:
            await db.execute(delete(Cabinet).where(Cabinet.id.in_(ids)))
            await db.commit()

    yield ids
    _run_db(_cleanup)


def _seed(cabinet, *children) -> int:
    """Insert the cabinet (+ children, cabinet_id patched) into the app DB."""

    async def _work(db):
        db.add(cabinet)
        await db.flush()
        for child in children:
            child.cabinet_id = cabinet.id
            db.add(child)
        await db.commit()
        return cabinet.id

    return _run_db(_work)


def _hours_mask(*on: int) -> list[int]:
    """24x0/1 mask with 1 at the given hours."""
    return [1 if hour in on else 0 for hour in range(24)]


def _log(cabinet_id: int, advert_id: int | None, action: str, status: str,
         detail: str | None = None) -> ControllerLog:
    return ControllerLog(
        cabinet_id=cabinet_id, advert_id=advert_id,
        action=action, status=status, detail=detail,
    )


def _seed_schedules_cabinet(cabinet_ids: list[int]) -> int:
    """Кабинет: cpc-кампания с расписанием+лимитом, cpm-кампания без, заказы.

    Заказы: 2 в час 10, 1 в час 22 (для генератора; coverage 90% выберет оба
    часа, 50% — только час 10).
    """
    today = msk_today()
    cabinet = make_cabinet(name="T6 Sched", wb_type="BY", currency="BYN")
    cid = _seed(
        cabinet,
        make_campaign(None, 6101, name="CPC РК", status=9, payment_type="cpc"),
        make_campaign(None, 6102, name="CPM РК", status=11, payment_type="cpm"),
        Schedule(
            cabinet_id=None, advert_id=6101,
            hours=_hours_mask(10, 11),
            daily_limit_kopecks=150000,
            enabled=True,
        ),
        make_order(None, "t6-a0", today, 10, price_kopecks=8000),
        make_order(None, "t6-a1", today, 10, price_kopecks=8000, nm_id=3),
        make_order(None, "t6-b", today, 22, price_kopecks=2000, nm_id=2),
        _log(None, 6101, "pause", "dry", "would pause: hour=3 schedule=off"),
        _log(None, 6102, "start", "ok", "hour=10 schedule=on"),
    )
    cabinet_ids.append(cid)
    return cid


# --- fake WbClient for POST apply (daily_limits_put must not fire in dry_run) ----


def _patch_limits_client(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Replace pages.WbClient; returns the list of daily_limits_put call args."""
    calls: list[tuple] = []

    class _FakeLimitsClient:
        def __init__(self, *args, **kwargs) -> None:
            self.put_calls = calls

        async def daily_limits_put(
            self,
            advert_ids: list[int],
            enabled: bool,
            daily_limit_kopecks: int | None = None,
            carry_over: bool | None = None,
        ) -> dict:
            self.put_calls.append(
                (list(advert_ids), enabled, daily_limit_kopecks, carry_over)
            )
            return {}

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(pages, "WbClient", _FakeLimitsClient)
    return calls


def _load_schedule(cabinet_id: int, advert_id: int) -> Schedule | None:
    async def _work(db):
        return await db.get(Schedule, (cabinet_id, advert_id))

    return _run_db(_work)


def _schedules_of(cabinet_id: int) -> dict[int, Schedule]:
    async def _work(db):
        rows = (
            (await db.execute(select(Schedule).where(Schedule.cabinet_id == cabinet_id)))
            .scalars()
            .all()
        )
        return {row.advert_id: row for row in rows}

    return _run_db(_work)


def _controller_logs(cabinet_id: int) -> list[ControllerLog]:
    async def _work(db):
        return list(
            (
                await db.execute(
                    select(ControllerLog)
                    .where(ControllerLog.cabinet_id == cabinet_id)
                    .order_by(ControllerLog.id)
                )
            )
            .scalars()
            .all()
        )

    return _run_db(_work)


def _setting(key: str):
    async def _work(db):
        return await get_setting(db, key, None)

    return _run_db(_work)


_CHECKED_HOURS_RE = re.compile(r'name="hour_(\d+)" value="1" checked')


def _checked_hours(html: str) -> set[int]:
    """Hours rendered as checked checkboxes (anywhere on the page)."""
    return {int(m) for m in _CHECKED_HOURS_RE.findall(html)}


# --- GET /schedules --------------------------------------------------------------


def test_schedules_page_renders_management_ui(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert r.status_code == 200
    assert "<h1>Расписания</h1>" in r.text
    assert 'href="/schedules"' in r.text  # nav link
    assert "T6 Sched" in r.text  # cabinet name
    # кампании таблицы: id, имя, payment_type badge
    assert "6101" in r.text and "CPC РК" in r.text
    assert "6102" in r.text and "CPM РК" in r.text
    assert ">cpc</span>" in r.text and ">cpm</span>" in r.text
    # 24 чекбокса на каждую кампанию
    assert r.text.count('name="hour_') == 48
    # форма применения POST на /schedules/{cabinet}/{advert}
    assert f'action="/schedules/{cid}/6101"' in r.text
    assert "Применить" in r.text
    # журнал контроллера внизу
    assert "Журнал контроллера" in r.text


def test_schedules_page_dry_run_banner_and_kill_switch_off(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert r.status_code == 200
    assert "режим: dry-run" in r.text  # баннер режима (spec: story 12)
    assert "Стоп всё" in r.text  # kill-switch выключен -> кнопка «Стоп всё»


def test_schedules_page_kill_switch_on_shows_resume(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)

    async def _on(db):
        await set_setting(db, KILL_SWITCH_KEY, True)

    _run_db(_on)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert r.status_code == 200
    assert "Возобновить" in r.text
    assert ">Стоп всё<" not in r.text


def test_schedules_page_live_banner(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)

    async def _live(db):
        await set_setting(db, MODE_KEY, "live")

    _run_db(_live)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert r.status_code == 200
    assert "режим: live" in r.text
    # перевод в live подтверждается onsubmit-диалогом
    assert "confirm(" in r.text


def test_schedules_page_checkboxes_prefilled_from_schedule(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    # расписание кампании 6101: часы 10 и 11 отмечены
    assert _checked_hours(r.text) == {10, 11}
    # дневной лимит: money-формат из daily_limit_kopecks (150000 -> "1500.00")
    assert 'value="1500.00"' in r.text
    # enabled у применённого расписания отмечен
    assert 'name="enabled" value="1" checked' in r.text


def test_schedules_page_shows_last_controller_action(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert "badge act-dry" in r.text  # статус последнего действия (badge)
    assert "pause" in r.text  # действие
    assert "только что" in r.text  # rel_time свежей записи


def test_schedules_page_shows_controller_log(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert "would pause: hour=3 schedule=off" in r.text
    assert "badge act-ok" in r.text and "badge act-dry" in r.text


def test_schedules_page_log_row_without_cabinet_renders(auth_client, cabinet_ids):
    """Глобальный журнал: строки без кабинета (служебные) не падают."""

    async def _add(db):
        db.add(ControllerLog(cabinet_id=None, advert_id=None, action="controller",
                             status="skipped", detail="kill-switch"))
        await db.commit()

    _run_db(_add)
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules", params={"cabinet": cid})
    assert r.status_code == 200
    assert "kill-switch" in r.text

    async def _cleanup(db):  # служебная строка не привязана к кабинету — чистим вручную
        await db.execute(
            delete(ControllerLog).where(ControllerLog.cabinet_id.is_(None),
                                        ControllerLog.action == "controller")
        )
        await db.commit()

    _run_db(_cleanup)


# --- GET /schedules: chooser, 404, auth -------------------------------------------


def test_schedules_without_cabinet_shows_chooser(auth_client, cabinet_ids):
    _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.get("/schedules")
    assert r.status_code == 200
    assert "T6 Sched" in r.text  # кабинет предложен как вариант выбора
    assert "/schedules?cabinet=" in r.text
    assert 'name="hour_' not in r.text  # без кабинета таблицы нет


def test_schedules_unknown_cabinet_is_404(auth_client):
    r = auth_client.get("/schedules", params={"cabinet": 424242})
    assert r.status_code == 404


def test_schedules_requires_login(client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = client.get(f"/schedules?cabinet={cid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_schedules_post_requires_login(client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = client.post(
        f"/schedules/{cid}/6101", data={"daily_limit": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- POST /schedules/{cabinet}/{advert}: применение расписания ---------------------


def test_apply_upserts_schedule_in_dry_run_without_wb_calls(
    auth_client, cabinet_ids, monkeypatch
):
    cid = _seed_schedules_cabinet(cabinet_ids)
    calls = _patch_limits_client(monkeypatch)

    r = auth_client.post(
        f"/schedules/{cid}/6101",
        data={"hour_10": "1", "hour_11": "1", "daily_limit": "150.50", "enabled": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/schedules?cabinet={cid}&applied={quote('CPC РК')}"

    schedule = _load_schedule(cid, 6101)
    assert schedule is not None
    assert schedule.hours[10] == 1 and schedule.hours[11] == 1
    assert schedule.hours[9] == 0  # неотмеченные часы = 0
    assert schedule.daily_limit_kopecks == 15050  # round(150.50 * 100)
    assert schedule.enabled is True
    assert schedule.applied_at is not None

    # dry_run: НИ ОДНОГО write-вызова к WB (spec: Global Constraints)
    assert calls == []
    # ... но действие журналируется со status="dry"
    limits = [log for log in _controller_logs(cid) if log.action == "limit"]
    assert len(limits) == 1 and limits[0].status == "dry"
    assert limits[0].advert_id == 6101

    # подтверждение на странице после редиректа
    page = auth_client.get(r.headers["location"])
    assert page.status_code == 200
    assert "CPC РК" in page.text


def test_apply_updates_existing_schedule(auth_client, cabinet_ids, monkeypatch):
    cid = _seed_schedules_cabinet(cabinet_ids)
    _patch_limits_client(monkeypatch)
    for limit, hours in (("100", (8, 9)), ("200", (12, 13, 14))):
        r = auth_client.post(
            f"/schedules/{cid}/6101",
            data={
                **{f"hour_{h}": "1" for h in hours},
                "daily_limit": limit,
                "enabled": "on",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    schedules = _schedules_of(cid)
    assert set(schedules) == {6101}  # upsert, а не вторая строка
    row = schedules[6101]
    assert row.hours == _hours_mask(12, 13, 14)
    assert row.daily_limit_kopecks == 20000


def test_apply_can_disable_schedule_and_clear_limit(auth_client, cabinet_ids, monkeypatch):
    cid = _seed_schedules_cabinet(cabinet_ids)
    _patch_limits_client(monkeypatch)
    r = auth_client.post(
        f"/schedules/{cid}/6101",
        data={"hour_10": "1", "daily_limit": ""},  # enabled не отмечен, лимит пустой
        follow_redirects=False,
    )
    assert r.status_code == 303
    schedule = _load_schedule(cid, 6101)
    assert schedule.enabled is False
    assert schedule.daily_limit_kopecks is None


def test_apply_bad_limit_is_422(auth_client, cabinet_ids, monkeypatch):
    cid = _seed_schedules_cabinet(cabinet_ids)
    _patch_limits_client(monkeypatch)
    for bad in ("abc", "-5"):
        r = auth_client.post(
            f"/schedules/{cid}/6101",
            data={"hour_10": "1", "daily_limit": bad, "enabled": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 422, f"daily_limit={bad!r} must be 422"

    schedules = _schedules_of(cid)
    assert set(schedules) == {6101}  # расписание cpm-кампании не создано
    assert schedules[6101].daily_limit_kopecks == 150000  # прежнее значение не тронуто


def test_apply_bad_hours_json_is_422(auth_client, cabinet_ids, monkeypatch):
    cid = _seed_schedules_cabinet(cabinet_ids)
    _patch_limits_client(monkeypatch)
    for bad_json in ("not json", "[1, 2, 3]", "[0, 2]"):
        r = auth_client.post(
            f"/schedules/{cid}/6102",
            data={"hours": bad_json, "daily_limit": ""},
            follow_redirects=False,
        )
        assert r.status_code == 422, f"hours={bad_json!r} must be 422"
    assert _load_schedule(cid, 6102) is None


def test_apply_accepts_hours_json(auth_client, cabinet_ids, monkeypatch):
    """Альтернативная форма часов: скрытый input hours с JSON-массивом 24x0/1."""
    cid = _seed_schedules_cabinet(cabinet_ids)
    _patch_limits_client(monkeypatch)
    mask = _hours_mask(21, 22)
    r = auth_client.post(
        f"/schedules/{cid}/6102",
        data={"hours": json.dumps(mask), "daily_limit": "50"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    schedule = _load_schedule(cid, 6102)
    assert schedule.hours == mask
    assert schedule.daily_limit_kopecks == 5000


def test_apply_live_calls_daily_limits_put_for_cpc(auth_client, cabinet_ids, monkeypatch):
    cid = _seed_schedules_cabinet(cabinet_ids)
    calls = _patch_limits_client(monkeypatch)

    async def _live(db):
        await set_setting(db, MODE_KEY, "live")

    _run_db(_live)
    r = auth_client.post(
        f"/schedules/{cid}/6101",
        data={"hour_10": "1", "hour_11": "1", "daily_limit": "150.50", "enabled": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # live: нативный лимит отправлен в WB ровно один раз
    assert calls == [([6101], True, 15050, True)]
    logs = [log for log in _controller_logs(cid) if log.action == "limit"]
    assert len(logs) == 1 and logs[0].status == "ok"


def test_apply_live_cpm_makes_no_native_call(auth_client, cabinet_ids, monkeypatch):
    """У CPM нативных дневных лимитов нет (живой факт: 400) — вызова не будет."""
    cid = _seed_schedules_cabinet(cabinet_ids)
    calls = _patch_limits_client(monkeypatch)

    async def _live(db):
        await set_setting(db, MODE_KEY, "live")

    _run_db(_live)
    r = auth_client.post(
        f"/schedules/{cid}/6102",
        data={"hour_10": "1", "daily_limit": "300", "enabled": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert calls == []  # cpm: без daily_limits_put даже в live
    schedule = _load_schedule(cid, 6102)
    assert schedule.daily_limit_kopecks == 30000  # лимит сохранён для контроля расхода


def test_apply_unknown_cabinet_or_campaign_is_404(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.post("/schedules/999999/6101", data={}, follow_redirects=False)
    assert r.status_code == 404
    r = auth_client.post(f"/schedules/{cid}/999999", data={}, follow_redirects=False)
    assert r.status_code == 404


# --- POST /schedules/generate: часы из тепловой карты ------------------------------


def test_generate_creates_disabled_schedules(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.post(f"/schedules/generate?cabinet={cid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/schedules?cabinet={cid}&generated=2"

    schedules = _schedules_of(cid)
    assert set(schedules) == {6101, 6102}  # обе кампании получили расписание
    for schedule in schedules.values():
        assert schedule.enabled is False  # генерация НЕ включает
        assert schedule.hours[10] == 1 and schedule.hours[22] == 1  # часы с заказами
        assert schedule.hours[11] == 0  # прежние отметки заменены генерацией
        assert sum(schedule.hours) == 6  # минимум 6 часов (генератор)
    # существующий лимит не затёрт генерацией
    assert schedules[6101].daily_limit_kopecks == 150000

    page = auth_client.get(r.headers["location"])
    assert page.status_code == 200
    assert "2 кампаний" in page.text  # подтверждение с числом


def test_generate_respects_coverage_setting(auth_client, cabinet_ids):
    """coverage_pct из app_settings: при 50% отбирается меньше часов, чем при 90%.

    Заказы в 7 разных часах (по 1): coverage 50 жадно берёт часы 8–11
    (400/700 >= 50%), coverage 90 требует все 7 (700/700 >= 90%). Добор до
    минимума 6 часов добавляет при 50% часы 12–13, но час 14 остаётся
    выключенным только при 50% — он и отличает настройки.
    """
    from tests.helpers import make_cabinet, make_campaign, make_order

    today = msk_today()
    cid = _seed(
        make_cabinet(name="T6 Coverage", wb_type="BY", currency="BYN"),
        make_campaign(None, 6201, name="Cov РК", status=9, payment_type="cpc"),
        *[make_order(None, f"t6-cov-{h}", today, h) for h in range(8, 15)],
    )
    cabinet_ids.append(cid)

    async def _coverage(db):
        await set_setting(db, COVERAGE_PCT_KEY, 50)

    _run_db(_coverage)
    r = auth_client.post(f"/schedules/generate?cabinet={cid}", follow_redirects=False)
    assert r.status_code == 303
    schedule = _schedules_of(cid)[6201]
    assert schedule.hours[8] == 1 and schedule.hours[11] == 1
    assert schedule.hours[14] == 0  # при 90% был бы 1
    assert sum(schedule.hours) == 6  # добор до минимума

    async def _default(db):
        await set_setting(db, COVERAGE_PCT_KEY, DEFAULT_COVERAGE_PCT)

    _run_db(_default)
    r = auth_client.post(f"/schedules/generate?cabinet={cid}", follow_redirects=False)
    assert r.status_code == 303
    schedule = _schedules_of(cid)[6201]
    assert all(schedule.hours[h] == 1 for h in range(8, 15))


def test_generate_unknown_cabinet_is_404(auth_client):
    r = auth_client.post("/schedules/generate?cabinet=424242", follow_redirects=False)
    assert r.status_code == 404


# --- POST /schedules/killswitch и /schedules/mode ----------------------------------


def test_killswitch_toggles(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.post(
        "/schedules/killswitch", data={"cabinet": str(cid)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/schedules?cabinet={cid}"

    page = auth_client.get("/schedules", params={"cabinet": cid})
    assert "Возобновить" in page.text
    assert _setting(KILL_SWITCH_KEY) is True
    # kill-switch здесь ничего не журналирует — это делает контроллер-тик
    assert all(log.action != "controller" for log in _controller_logs(cid))

    r = auth_client.post(
        "/schedules/killswitch", data={"cabinet": str(cid)}, follow_redirects=False
    )
    assert r.status_code == 303
    page = auth_client.get("/schedules", params={"cabinet": cid})
    assert "Стоп всё" in page.text
    assert _setting(KILL_SWITCH_KEY) is False  # вернулись к выключенному


def test_mode_toggles(auth_client, cabinet_ids):
    cid = _seed_schedules_cabinet(cabinet_ids)
    r = auth_client.post(
        "/schedules/mode", data={"cabinet": str(cid)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/schedules?cabinet={cid}"
    page = auth_client.get("/schedules", params={"cabinet": cid})
    assert "режим: live" in page.text
    assert _setting(MODE_KEY) == "live"

    r = auth_client.post(
        "/schedules/mode", data={"cabinet": str(cid)}, follow_redirects=False
    )
    assert r.status_code == 303
    page = auth_client.get("/schedules", params={"cabinet": cid})
    assert "режим: dry-run" in page.text
    assert _setting(MODE_KEY) == "dry_run"


def test_killswitch_without_cabinet_redirects_to_schedules(auth_client):
    r = auth_client.post("/schedules/killswitch", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/schedules"
