"""Collector jobs: one collect_* function per loop + loop assembly (start_all).

Every collect_* takes (client, session, cabinet_id), performs ONLY read calls
on the WB client, writes through app.repos (each call commits) and returns
the number of processed rows for collection_log detail.

Raw responses: every endpoint hit is journaled into raw_responses via
save_raw (retention = Settings.raw_log_days; 0 disables writing). Response
formats marked NOT confirmed are stored raw and parsed defensively — see
.scratch/facts.md (the spike got 403 everywhere).
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.loops import IntervalLoop, Sleep
from app.collector.windows import next_window
from app.config import get_settings
from app.crypto import decrypt_token
from app.db import SessionLocal
from app.models import BidHistory, Cabinet, Campaign, CollectionLog, RawResponse
from app.repos import (
    append_bids,
    insert_balance,
    insert_budgets,
    insert_snapshot,
    log_collection,
    upsert_campaigns,
    upsert_clusters,
    upsert_positions,
    upsert_stats,
)
from app.wb.client import WbClient
from app.wb.errors import WbApiError
from app.wb.limiter import LimiterRegistry
from app.wb.parsers import get_any, parse_adverts, parse_budget, parse_fullstats

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))  # stat dates are Moscow-calendar days
_MAX_BATCH_IDS = 50  # WB adverts/budget accept <= 50 ids per request
_ACTIVE_STATUSES = (9, 11)  # WB campaign statuses considered running

# Raw-response endpoint labels (same strings as the client's paths).
_ENDPOINT_COUNT = "/adv/v1/promotion/count"
_ENDPOINT_ADVERTS = "/api/advert/v2/adverts"
_ENDPOINT_BUDGET = "/api/advert/v2/budget"
_ENDPOINT_BALANCE = "/adv/v1/balance"
_ENDPOINT_FULLSTATS = "/adv/v3/fullstats"
_ENDPOINT_BIDS_MIN = "/api/advert/v1/bids/min"
_ENDPOINT_RECOMMENDATIONS = "/api/advert/v0/bids/recommendations"
_ENDPOINT_NORMQUERY_LIST = "/adv/v0/normquery/list"
_ENDPOINT_NORMQUERY_STATS = "/adv/v1/normquery/stats"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _chunked(items: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def save_raw(
    session: AsyncSession, cabinet_id: int, endpoint: str, status: int, payload: Any
) -> None:
    """Journal one raw API response (own insert+commit); raw_log_days=0 disables."""
    if get_settings().raw_log_days <= 0:
        return
    await session.execute(
        insert(RawResponse).values(
            cabinet_id=cabinet_id,
            endpoint=endpoint,
            status=status,
            payload=payload,
            captured_at=_now_utc(),
        )
    )
    await session.commit()


# -- collection jobs ---------------------------------------------------------


async def collect_campaigns(client: WbClient, session: AsyncSession, cabinet_id: int) -> int:
    """promotion/count -> ALL advert ids (every status) -> adverts in <=50 batches."""
    count_payload = await client.promotion_count()
    await save_raw(session, cabinet_id, _ENDPOINT_COUNT, 200, count_payload)
    # NB: all statuses, not only active — pauses/finishings must stay visible.
    advert_ids, types = _advert_ids_and_types_from_count(count_payload)

    rows: list[dict] = []
    for chunk in _chunked(advert_ids, _MAX_BATCH_IDS):
        payload = await client.adverts(chunk)
        await save_raw(session, cabinet_id, _ENDPOINT_ADVERTS, 200, payload)
        rows.extend(parse_adverts(payload, types))

    await upsert_campaigns(session, cabinet_id, rows)
    await append_bids(session, cabinet_id, rows, captured_at=_now_utc())
    return len(rows)


async def collect_budgets(client: WbClient, session: AsyncSession, cabinet_id: int) -> int:
    """Budgets of active campaigns (status 9/11) from the DB, <=50 ids per call."""
    advert_ids = list(
        (
            await session.execute(
                select(Campaign.advert_id)
                .where(
                    Campaign.cabinet_id == cabinet_id,
                    Campaign.status.in_(_ACTIVE_STATUSES),
                )
                .order_by(Campaign.advert_id)
            )
        ).scalars()
    )
    written = 0
    captured_at = _now_utc()
    for chunk in _chunked(advert_ids, _MAX_BATCH_IDS):
        payload = await client.budgets(chunk)
        await save_raw(session, cabinet_id, _ENDPOINT_BUDGET, 200, payload)
        written += await insert_budgets(
            session, cabinet_id, parse_budget(payload), captured_at
        )
    return written


async def collect_balance(client: WbClient, session: AsyncSession, cabinet_id: int) -> int:
    """One balance observation; field naming not confirmed (get_any both)."""
    payload = await client.balance()
    await save_raw(session, cabinet_id, _ENDPOINT_BALANCE, 200, payload)
    total = int(get_any(payload, "total", "balance", default=0))
    return await insert_balance(session, cabinet_id, total, _now_utc())


async def collect_fullstats(client: WbClient, session: AsyncSession, cabinet_id: int) -> int:
    """Per active campaign: the FULL 31-day window every pass, stats upsert.

    Живой факт: WB уточняет статистику задним числом (атрибуция 7 дней), а
    скользящее окно «от прошлого успеха» навсегда теряло дни, уже попавшие в
    прошлый проход. Полное окно каждый проход дороже по лимиту (fullstats
    1 req/мин → 20 кампаний ≈ 20 минут ≈ период цикла), зато история не рвётся.
    Порядок — по advert_id. WbApiError на ОДНОЙ кампании пишется "error", проход
    продолжается.
    """
    advert_ids = list(
        (
            await session.execute(
                select(Campaign.advert_id)
                .where(
                    Campaign.cabinet_id == cabinet_id,
                    Campaign.status.in_(_ACTIVE_STATUSES),
                )
                .order_by(Campaign.advert_id)
            )
        ).scalars()
    )

    today_msk = datetime.now(_MSK).date()
    begin, end = next_window(None, datetime.now(_MSK))  # always the full 31 days

    written = 0
    for advert_id in advert_ids:
        try:
            payload = await client.fullstats([advert_id], begin, end)
        except WbApiError as exc:
            logger.warning("fullstats %s (cabinet %s) failed: %s", advert_id, cabinet_id, exc)
            await log_collection(
                session, cabinet_id, f"fullstats:{advert_id}", "error", str(exc),
                None, _now_utc(),
            )
            continue
        await save_raw(session, cabinet_id, _ENDPOINT_FULLSTATS, 200, payload)
        stat_rows, position_rows = parse_fullstats(payload)
        written += await upsert_stats(session, cabinet_id, stat_rows)
        written += await upsert_positions(session, cabinet_id, position_rows)
        await log_collection(
            session, cabinet_id, f"fullstats:{advert_id}", "ok",
            f"stats={len(stat_rows)} positions={len(position_rows)}",
            None, _now_utc(),
        )
        if datetime.now(_MSK).date() != today_msk:  # midnight crossed mid-pass
            today_msk = datetime.now(_MSK).date()
            begin, end = next_window(None, datetime.now(_MSK))
    return written


async def collect_bids_min(client: WbClient, session: AsyncSession, cabinet_id: int) -> int:
    """Minimal bids for every nm ever seen in bid_history; response kept raw.

    The bids/min response format is NOT confirmed — store the whole payload as
    an auction snapshot (parsed={"kind": "bids_min"}); advert_id=0 marks a
    cabinet-level snapshot no single campaign owns. No bids yet -> skip.
    """
    nm_ids = list(
        (
            await session.execute(
                select(BidHistory.nm_id)
                .where(BidHistory.cabinet_id == cabinet_id)
                .distinct()
                .order_by(BidHistory.nm_id)
            )
        ).scalars()
    )
    if not nm_ids:
        return 0
    payload = await client.bids_min(nm_ids)
    await save_raw(session, cabinet_id, _ENDPOINT_BIDS_MIN, 200, payload)
    await insert_snapshot(
        session, cabinet_id, advert_id=0, payload=payload,
        parsed={"kind": "bids_min"}, captured_at=_now_utc(),
    )
    return 1


async def collect_auction_snapshot(
    client: WbClient, session: AsyncSession, cabinet_id: int
) -> int:
    """Recommendations snapshot per ACTIVE manual+cpm campaign.

    Only manual+cpm campaigns are queried: recommendations make sense for
    auction placements; unified/cpc campaigns are skipped (their bid is a
    single unified value — nothing per-placement to snapshot). Parameters are
    NOT confirmed by the spike; on a 4xx the error body is logged "error" and
    the pass continues, other errors propagate to the IntervalLoop.
    """
    campaigns = (
        (
            await session.execute(
                select(Campaign)
                .where(
                    Campaign.cabinet_id == cabinet_id,
                    Campaign.status.in_(_ACTIVE_STATUSES),
                )
                .order_by(Campaign.advert_id)
            )
        )
        .scalars()
        .all()
    )
    written = 0
    for campaign in campaigns:
        if campaign.bid_type != "manual" or campaign.payment_type != "cpm":
            continue  # unified/cpc — no auction recommendations to snapshot
        try:
            payload = await client.recommendations(
                {
                    "advertId": campaign.advert_id,
                    "type": campaign.type,
                    "payment_type": "cpm",
                }
            )
        except WbApiError as exc:
            if not 400 <= exc.status < 500:
                raise  # 5xx/network — let the loop's backoff handle it
            logger.warning(
                "recommendations %s (cabinet %s) failed: %s",
                campaign.advert_id, cabinet_id, exc,
            )
            await log_collection(
                session, cabinet_id, f"auction_snapshot:{campaign.advert_id}",
                "error", str(exc), None, _now_utc(),
            )
            continue
        await save_raw(session, cabinet_id, _ENDPOINT_RECOMMENDATIONS, 200, payload)
        await insert_snapshot(
            session, cabinet_id, campaign.advert_id, payload=payload,
            parsed={"kind": "recommendations"}, captured_at=_now_utc(),
        )
        written += 1
    return written


# phrase-ish keys accepted in normquery payloads (naming NOT confirmed)
_PHRASE_KEYS = ("phrase", "normquery", "norm_query", "query", "keyword")
_LIST_KEYS = ("data", "phrases", "queries", "normqueries", "items")


def _extract_phrases(payload: Any) -> list[str]:
    """Best-effort phrase list from a normquery/list payload of unknown shape."""
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = next(
            (value for key in _LIST_KEYS if isinstance(value := payload.get(key), list)),
            [],
        )
    else:
        candidates = []
    phrases: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            phrases.append(item)
        elif isinstance(item, dict):
            for key in _PHRASE_KEYS:
                if item.get(key):
                    phrases.append(str(item[key]))
                    break
    return phrases


async def collect_clusters(client: WbClient, session: AsyncSession, cabinet_id: int) -> int:
    """Clusters of manual-bid campaigns: phrase list -> phrase stats.

    Formats are NOT confirmed: phrases are extracted defensively and the raw
    stats response is stored as-is in clusters.stats jsonb (refine once live
    responses arrive). 4xx on either call is logged "error" and the pass
    continues; other errors propagate to the IntervalLoop.
    """
    advert_ids = list(
        (
            await session.execute(
                select(Campaign.advert_id)
                .where(
                    Campaign.cabinet_id == cabinet_id,
                    Campaign.status.in_(_ACTIVE_STATUSES),
                    Campaign.bid_type == "manual",
                )
                .order_by(Campaign.advert_id)
            )
        ).scalars()
    )
    written = 0
    for advert_id in advert_ids:
        try:
            list_payload = await client.normquery_list(advert_id)
            await save_raw(session, cabinet_id, _ENDPOINT_NORMQUERY_LIST, 200, list_payload)
            queries = _extract_phrases(list_payload)
            if not queries:
                continue
            stats_payload = await client.normquery_stats(
                {"advertId": advert_id, "queries": queries}
            )
            await save_raw(session, cabinet_id, _ENDPOINT_NORMQUERY_STATS, 200, stats_payload)
        except WbApiError as exc:
            if not 400 <= exc.status < 500:
                raise
            logger.warning(
                "normquery %s (cabinet %s) failed: %s", advert_id, cabinet_id, exc
            )
            await log_collection(
                session, cabinet_id, f"clusters:{advert_id}", "error", str(exc),
                None, _now_utc(),
            )
            continue
        # raw stats response kept per phrase until the format is confirmed
        rows = [{"norm_query": query, "stats": stats_payload} for query in queries]
        written += await upsert_clusters(session, cabinet_id, advert_id, rows)
    return written


async def cleanup_raw_responses(session_factory: Callable[[], Any], keep_days: int) -> int:
    """Delete raw_responses older than keep_days; returns the rows deleted."""
    cutoff = _now_utc() - timedelta(days=keep_days)
    async with session_factory() as session:
        result = await session.execute(
            delete(RawResponse).where(RawResponse.captured_at < cutoff)
        )
        await session.commit()
        return int(result.rowcount)


# -- helpers -----------------------------------------------------------------


def _advert_ids_and_types_from_count(payload: dict) -> tuple[list[int], dict[int, int]]:
    """All advert ids from promotion/count across type groups and ALL statuses.

    Живой факт (спайк 13.10.2026): adverts[].advert_list[].advertId (тип —
    на группе). Legacy-форма adverts[].chards[].advertList из старого API
    тоже принимается (плоский список int). Возвращает (ids, {id: type}).
    """
    ids: list[int] = []
    types: dict[int, int] = {}
    seen: set[int] = set()
    for group in get_any(payload, "adverts", default=()) or ():
        group_type = _to_int_safe(get_any(group, "type"))
        # legacy: chards[].advertList = [int, ...]
        for chard in get_any(group, "chards", default=()) or ():
            for advert_id in get_any(chard, "advertList", "advert_list", default=()) or ():
                if isinstance(advert_id, Mapping):
                    advert_id = get_any(advert_id, "advertId", "advert_id")
                advert_id = _to_int_safe(advert_id)
                if advert_id not in seen:
                    seen.add(advert_id)
                    ids.append(advert_id)
                    types[advert_id] = group_type
        # live: advert_list[] = [{"advertId": int, ...}, ...]
        for item in get_any(group, "advertList", "advert_list", default=()) or ():
            if isinstance(item, Mapping):
                advert_id = _to_int_safe(get_any(item, "advertId", "advert_id"))
            else:
                advert_id = _to_int_safe(item)
            if advert_id and advert_id not in seen:
                seen.add(advert_id)
                ids.append(advert_id)
                types[advert_id] = group_type
    return ids, types


def _to_int_safe(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def _last_fullstats_success(
    session: AsyncSession, cabinet_id: int, advert_id: int
) -> datetime | None:
    """Last successful fullstats finished_at for one campaign (collection_log)."""
    result = await session.execute(
        select(CollectionLog.finished_at)
        .where(
            CollectionLog.cabinet_id == cabinet_id,
            CollectionLog.loop_name == f"fullstats:{advert_id}",
            CollectionLog.status == "ok",
            CollectionLog.finished_at.is_not(None),
        )
        .order_by(CollectionLog.finished_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# -- one-off manual sync (the admin page's "Обновить сейчас" button) ----------

_SYNC_STEPS: tuple[tuple[str, Callable[[WbClient, AsyncSession, int], Any]], ...] = (
    ("campaigns", collect_campaigns),
    ("budgets", collect_budgets),
    ("balance", collect_balance),
    ("fullstats", collect_fullstats),
)

_DETAIL_MAX_LEN = 200


async def sync_cabinet_once(cabinet_id: int) -> dict[str, int | str]:
    """One synchronous read-only collection pass over a cabinet (manual refresh).

    Runs campaigns, budgets, balance and fullstats in order, each in its own
    session and its own try/except WbApiError: a failed step becomes the string
    status "ошибка: <detail>" and the remaining steps still run. Every step
    appends a collection_log row (the same bookkeeping IntervalLoop does for
    the background loops) so the admin page's freshness reflects manual syncs
    too. Raises ValueError when the cabinet does not exist.
    """
    async with SessionLocal() as session:
        cabinet = await session.get(Cabinet, cabinet_id)
    if cabinet is None:
        raise ValueError(f"cabinet {cabinet_id} not found")

    client = WbClient(
        decrypt_token(cabinet.token_encrypted),
        LimiterRegistry(),
        cabinet_id=cabinet_id,
    )
    result: dict[str, int | str] = {}
    try:
        for name, collect in _SYNC_STEPS:
            started_at = _now_utc()
            try:
                async with SessionLocal() as session:
                    processed = await collect(client, session, cabinet_id)
            except WbApiError as exc:
                detail = str(exc)[:_DETAIL_MAX_LEN]
                result[name] = f"ошибка: {detail}"
                async with SessionLocal() as session:
                    await log_collection(
                        session, cabinet_id, name, "error", detail,
                        started_at, _now_utc(),
                    )
            else:
                result[name] = processed
                async with SessionLocal() as session:
                    await log_collection(
                        session, cabinet_id, name, "ok", f"processed={processed}",
                        started_at, _now_utc(),
                    )
    finally:
        await client.aclose()
    return result


# -- loop assembly -----------------------------------------------------------


@dataclass(frozen=True)
class LoopSpec:
    """One collection loop: its collector function name and period."""

    name: str
    period_sec: int


LOOPS_SPEC: list[LoopSpec] = [
    LoopSpec("campaigns", 300),
    LoopSpec("budgets", 300),
    LoopSpec("balance", 300),
    LoopSpec("fullstats", 1200),
    LoopSpec("bids_min", 86400),
    LoopSpec("auction_snapshot", 3600),
    LoopSpec("clusters", 86400),
]

_CLEANUP_NAME = "cleanup"
_CLEANUP_PERIOD_SEC = 86400

_COLLECTORS: dict[str, Callable[[WbClient, AsyncSession, int], Any]] = {
    "campaigns": collect_campaigns,
    "budgets": collect_budgets,
    "balance": collect_balance,
    "fullstats": collect_fullstats,
    "bids_min": collect_bids_min,
    "auction_snapshot": collect_auction_snapshot,
    "clusters": collect_clusters,
}

# process-wide registry so stop_all can cancel loops / close clients
_tasks: list[asyncio.Task] = []
_clients: list[WbClient] = []

# supervisor state: per-cabinet loop entries (task, loop, token fingerprint)
# plus the one client all of a cabinet's loops share. The fingerprint is
# sha256 of token_encrypted — a token change is detected WITHOUT decrypting.
_running: dict[int, list[tuple[asyncio.Task, IntervalLoop, str]]] = {}
_cabinet_clients: dict[int, WbClient] = {}
_supervisor_task: asyncio.Task | None = None


def _bind_collect(
    client: WbClient,
    cabinet_id: int,
    collect: Callable[[WbClient, AsyncSession, int], Any],
) -> Callable[[], Any]:
    """Wrap collect_* into the no-arg fn IntervalLoop expects."""

    async def fn() -> int | None:
        async with SessionLocal() as session:
            return await collect(client, session, cabinet_id)

    return fn


def _bind_cleanup() -> Callable[[], Any]:
    async def fn() -> int | None:
        keep_days = get_settings().raw_log_days
        if keep_days <= 0:
            return 0
        return await cleanup_raw_responses(SessionLocal, keep_days)

    return fn


def _token_fingerprint(token_encrypted: str) -> str:
    """sha256 of the ENCRYPTED token: detects a token change without decrypting."""
    return hashlib.sha256(token_encrypted.encode()).hexdigest()


async def start_for_cabinet(cabinet: Cabinet) -> list[IntervalLoop]:
    """Build and start the LOOPS_SPEC collection loops for one cabinet.

    Registers the loop tasks in _running (keyed by cabinet id, each entry
    carrying the token fingerprint) and the shared client in _cabinet_clients.
    """
    fingerprint = _token_fingerprint(cabinet.token_encrypted)
    client = WbClient(
        decrypt_token(cabinet.token_encrypted),
        LimiterRegistry(),
        cabinet_id=cabinet.id,
    )
    _cabinet_clients[cabinet.id] = client
    loops: list[IntervalLoop] = []
    entries: list[tuple[asyncio.Task, IntervalLoop, str]] = []
    for spec in LOOPS_SPEC:
        loop = IntervalLoop(
            spec.name,
            cabinet.id,
            spec.period_sec,
            _bind_collect(client, cabinet.id, _COLLECTORS[spec.name]),
        )
        loops.append(loop)
        task = asyncio.create_task(
            loop.run_forever(), name=f"collector:{spec.name}:{cabinet.id}"
        )
        entries.append((task, loop, fingerprint))
    _running[cabinet.id] = entries
    logger.info(
        "collector: started %d loops for cabinet %d (%s)",
        len(loops), cabinet.id, cabinet.name,
    )
    return loops


async def stop_for_cabinet(cabinet_id: int) -> None:
    """Cancel one cabinet's loop tasks and close its client; no-op if not running."""
    entries = _running.pop(cabinet_id, None)
    client = _cabinet_clients.pop(cabinet_id, None)
    if not entries and client is None:
        return
    tasks = [task for task, _loop, _fp in entries or []]
    for task, loop, _fp in entries or []:
        loop.stopped = True
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if client is not None:
        await client.aclose()
    logger.info("collector: stopped cabinet %d (%d loops)", cabinet_id, len(entries or ()))


async def supervisor_tick() -> int:
    """Reconcile the running loops with the cabinets table; return the change count.

    A cabinet deleted or deactivated since the last tick gets its loops stopped;
    a new (or re-activated) cabinet gets loops started; a changed token
    (different ciphertext fingerprint) restarts the cabinet's loops so the new
    token is used without waiting for a process restart.
    """
    async with SessionLocal() as session:
        cabinets = (await session.execute(select(Cabinet))).scalars().all()
    active = {cabinet.id: cabinet for cabinet in cabinets if cabinet.active}

    changes = 0
    for cabinet_id in list(_running):
        if cabinet_id not in active:
            await stop_for_cabinet(cabinet_id)
            changes += 1
    for cabinet_id, cabinet in active.items():
        fingerprint = _token_fingerprint(cabinet.token_encrypted)
        entries = _running.get(cabinet_id)
        if entries is not None and all(fp == fingerprint for _, _, fp in entries):
            continue  # already running with the current token
        if entries is not None:  # token changed: restart with the new token
            await stop_for_cabinet(cabinet_id)
        try:
            await start_for_cabinet(cabinet)
        except ValueError as exc:  # undecryptable token, e.g. the Fernet key rotated
            logger.error(
                "supervisor: cabinet %d (%s) skipped, %s — "
                "замените токен на /admin/cabinets (Заменить токен)",
                cabinet_id, cabinet.name, exc,
            )
            changes += 1
            continue
        changes += 1
    if changes:
        logger.info("supervisor: %d cabinet change(s)", changes)
    return changes


async def supervisor_forever(*, sleep: Sleep = asyncio.sleep, period_sec: int = 60) -> None:
    """supervisor_tick every period_sec; the first tick runs immediately.

    A failing tick (e.g. a DB blip) is logged and retried on the next period —
    the supervisor must outlive transient errors.
    """
    while True:
        try:
            await supervisor_tick()
        except Exception:
            logger.exception("supervisor tick failed")
        await sleep(period_sec)


async def start_all() -> list[IntervalLoop]:
    """Build and start one IntervalLoop per (active cabinet x LOOPS_SPEC) plus
    the cabinetless nightly raw_responses cleaner and the cabinet supervisor
    (picks up cabinets added after startup). Returns the loop list."""
    loops: list[IntervalLoop] = []
    async with SessionLocal() as session:
        cabinets = (
            (await session.execute(select(Cabinet).where(Cabinet.active.is_(True))))
            .scalars()
            .all()
        )
    for cabinet in cabinets:
        loops.extend(await start_for_cabinet(cabinet))
    loops.append(IntervalLoop(_CLEANUP_NAME, None, _CLEANUP_PERIOD_SEC, _bind_cleanup()))
    _tasks.append(
        asyncio.create_task(loops[-1].run_forever(), name="collector:cleanup")
    )
    global _supervisor_task
    _supervisor_task = asyncio.create_task(supervisor_forever(), name="collector:supervisor")
    logger.info(
        "collector started: %d loops for %d cabinets + supervisor", len(loops), len(cabinets)
    )
    return loops


async def stop_all(loops: list[IntervalLoop]) -> None:
    """Cancel all collector tasks (loops, cleaner, supervisor) and close clients."""
    for loop in loops:
        loop.stopped = True
    global _supervisor_task
    supervisor, _supervisor_task = _supervisor_task, None
    tasks = list(_tasks)
    _tasks.clear()
    if supervisor is not None:
        tasks.append(supervisor)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for cabinet_id in list(_running):
        await stop_for_cabinet(cabinet_id)
    clients = list(_clients)
    _clients.clear()
    for client in clients:
        await client.aclose()
    logger.info("collector stopped: %d loops", len(loops))
