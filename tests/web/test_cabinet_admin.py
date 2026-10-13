"""Cabinet admin tests (hotfix): add-feedback, manual sync, token replace, delete.

Same DB strategy as tests/web/test_pages.py: seeding and verification run
against the app database (app.db.SessionLocal) through a throwaway engine;
runner.WbClient is monkeypatched with an offline fake, so POST /sync performs
a real collection pass writing real collection_log rows without network.
"""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient  # noqa: F401  -- fixtures come from conftest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.collector import runner
from app.crypto import decrypt_token
from app.models import Cabinet, Campaign, CollectionLog
from tests.helpers import DEFAULT_TOKEN, make_cabinet, make_campaign, make_collection_log

RAW_TOKEN = "x-admin-raw-token"
NEW_TOKEN = "x-admin-replaced-token"


class _FakeWb:
    """Offline WbClient stand-in shaped like the FakeClient in test_runner_integration."""

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[str] = []

    async def promotion_count(self) -> dict:
        self.calls.append("promotion_count")
        return {"all": 0, "adverts": []}

    async def adverts(self, ids: list[int]) -> list[dict]:
        self.calls.append("adverts")
        return []

    async def budgets(self, advert_ids: list[int]) -> list[dict]:
        self.calls.append("budgets")
        return []

    async def balance(self) -> dict:
        self.calls.append("balance")
        return {"total": 100}

    async def fullstats(self, advert_ids: list[int], begin, end) -> list[dict]:
        self.calls.append("fullstats")
        return []

    async def bids_min(self, nm_ids: list[int]) -> dict:
        self.calls.append("bids_min")
        return {}

    async def recommendations(self, params: dict) -> dict:
        self.calls.append("recommendations")
        return {}

    async def normquery_list(self, advert_id: int) -> dict:
        self.calls.append("normquery_list")
        return {}

    async def normquery_stats(self, body: dict) -> dict:
        self.calls.append("normquery_stats")
        return {}

    async def aclose(self) -> None:
        self.calls.append("aclose")


@pytest.fixture
def fake_wb(monkeypatch: pytest.MonkeyPatch) -> _FakeWb:
    """Replace runner.WbClient so sync_cabinet_once never touches the network."""
    fake = _FakeWb()
    monkeypatch.setattr(runner, "WbClient", lambda *a, **kw: fake)
    return fake


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


def _seed(cabinet: Cabinet, *children) -> int:
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


# --- POST /admin/cabinets: feedback after adding ---------------------------------


def test_create_redirects_with_added_banner(auth_client, cabinet_ids):
    r = auth_client.post(
        "/admin/cabinets",
        data={"name": "Admin Add RU", "wb_type": "RU", "token": RAW_TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/cabinets?added=")

    page = auth_client.get(r.headers["location"])
    assert page.status_code == 200
    assert "Admin Add RU" in page.text
    assert "добавлен" in page.text
    assert "Обновить сейчас" in page.text  # where the first collection comes from

    async def _load(db):
        return (
            (await db.execute(select(Cabinet).where(Cabinet.name == "Admin Add RU")))
            .scalars()
            .one()
        )

    cab = _run_db(_load)
    cabinet_ids.append(cab.id)
    assert decrypt_token(cab.token_encrypted) == RAW_TOKEN


def test_admin_page_shows_token_hint_and_phase_note(auth_client):
    page = auth_client.get("/admin/cabinets")
    assert page.status_code == 200
    assert "Доступ к API" in page.text  # where to create the token
    assert "Персональный" in page.text
    assert "Продвижение" in page.text
    # honest answer about the bidder: phase 1 is read-only
    assert "только чтение" in page.text
    assert "биддера" in page.text


# --- POST /admin/cabinets/{id}/sync: manual refresh ------------------------------


def test_sync_runs_collection_and_shows_fresh_status(
    auth_client, cabinet_ids, fake_wb: _FakeWb
):
    cid = _seed(
        make_cabinet(name="Admin Sync", token=RAW_TOKEN),
        make_campaign(None, 4001, name="Sync РК"),
    )
    cabinet_ids.append(cid)

    r = auth_client.post(f"/admin/cabinets/{cid}/sync", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/cabinets?synced=1"
    # empty promotion/count -> no adverts call; the seeded campaign 4001 is
    # picked up by budgets/fullstats straight from the DB
    assert fake_wb.calls == ["promotion_count", "budgets", "balance", "fullstats", "aclose"]
    assert fake_wb.calls[-1] == "aclose"  # client closed after the pass

    async def _logs(db):
        return (
            (
                await db.execute(
                    select(CollectionLog).where(CollectionLog.cabinet_id == cid)
                )
            )
            .scalars()
            .all()
        )

    logs = _run_db(_logs)
    loop_names = {log.loop_name for log in logs}
    assert {"campaigns", "budgets", "balance", "fullstats", "fullstats:4001"} <= loop_names
    assert all(log.status == "ok" for log in logs)

    page = auth_client.get("/admin/cabinets", params={"synced": "1"})
    assert page.status_code == 200
    assert "только что" in page.text  # last successful collection is right now


def test_sync_unknown_cabinet_is_404(auth_client):
    r = auth_client.post("/admin/cabinets/999999/sync", follow_redirects=False)
    assert r.status_code == 404


# --- POST /admin/cabinets/{id}/token: replace a wrong token ----------------------


def test_token_replace_updates_encrypted_token(auth_client, cabinet_ids):
    cid = _seed(make_cabinet(name="Admin Token", token=RAW_TOKEN))
    cabinet_ids.append(cid)

    r = auth_client.post(
        f"/admin/cabinets/{cid}/token",
        data={"token": NEW_TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def _load(db):
        return await db.get(Cabinet, cid)

    cab = _run_db(_load)
    assert decrypt_token(cab.token_encrypted) == NEW_TOKEN
    assert cab.name == "Admin Token"  # name untouched when not submitted


def test_token_replace_can_rename_cabinet(auth_client, cabinet_ids):
    cid = _seed(make_cabinet(name="Old Name", token=RAW_TOKEN))
    cabinet_ids.append(cid)
    r = auth_client.post(
        f"/admin/cabinets/{cid}/token",
        data={"token": NEW_TOKEN, "name": "New Name"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def _load(db):
        return await db.get(Cabinet, cid)

    cab = _run_db(_load)
    assert cab.name == "New Name"
    assert decrypt_token(cab.token_encrypted) == NEW_TOKEN


def test_token_replace_unknown_cabinet_is_404(auth_client):
    r = auth_client.post(
        "/admin/cabinets/999999/token",
        data={"token": NEW_TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 404


# --- POST /admin/cabinets/{id}/delete: remove with cascade ------------------------


def test_delete_removes_cabinet_and_children(auth_client, cabinet_ids):
    cid = _seed(
        make_cabinet(name="Admin Delete", token=RAW_TOKEN),
        make_campaign(None, 4002, name="Doomed РК"),
    )
    cabinet_ids.append(cid)
    cabinet_ids.append(cid)

    r = auth_client.post(f"/admin/cabinets/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/cabinets"

    async def _counts(db):
        cabinets = await db.scalar(
            select(func.count()).select_from(Cabinet).where(Cabinet.id == cid)
        )
        campaigns = await db.scalar(
            select(func.count()).select_from(Campaign).where(Campaign.cabinet_id == cid)
        )
        return cabinets, campaigns

    cabinets, campaigns = _run_db(_counts)
    assert (cabinets, campaigns) == (0, 0)  # cascade removed the campaign too
    cabinet_ids.remove(cid)  # teardown must not double-delete

    page = auth_client.get("/admin/cabinets")
    assert "Admin Delete" not in page.text


def test_delete_unknown_cabinet_is_404(auth_client):
    r = auth_client.post("/admin/cabinets/999999/delete", follow_redirects=False)
    assert r.status_code == 404


# --- admin list: last collection status per cabinet --------------------------------


def test_admin_page_lists_last_run_status(auth_client, cabinet_ids):
    cid = _seed(
        make_cabinet(name="Admin Status", token=DEFAULT_TOKEN),
        make_collection_log(None, loop_name="campaigns", status="error"),
    )
    cabinet_ids.append(cid)
    page = auth_client.get("/admin/cabinets")
    assert page.status_code == 200
    assert "Admin Status" in page.text
    assert 'badge loop-error">ошибка' in page.text

    _seed_log(cid, "budgets", "ok")  # a newer successful run flips the badge
    page = auth_client.get("/admin/cabinets")
    assert 'badge loop-ok">ok' in page.text


def _seed_log(cabinet_id: int, loop_name: str, status: str) -> None:
    async def _work(db):
        db.add(
            CollectionLog(cabinet_id=cabinet_id, loop_name=loop_name, status=status)
        )
        await db.commit()

    _run_db(_work)
