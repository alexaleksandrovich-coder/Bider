"""Thin async client for the WB advert-api (read-only endpoints only).

Every request first goes through the per-(cabinet, class) token bucket, then
out with ``Authorization: <token>`` — WB uses the raw token, no "Bearer"
prefix. A 429 is retried exactly once after X-Ratelimit-Retry seconds (5 by
default); a second 429, any other non-2xx and network-level errors raise
WbApiError. Successful 2xx responses are returned as parsed JSON.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, Self

import httpx

from app.wb.constants import HOST, klass
from app.wb.errors import WbApiError
from app.wb.limiter import LimiterRegistry

logger = logging.getLogger(__name__)

RetrySleep = Callable[[float], Awaitable[None]]

_MAX_ADVERTS_IDS = 50
_MAX_DAILY_LIMIT_IDS = 100
_DEFAULT_RETRY_AFTER = 5.0
_NO_CHANGE_MARK = "Статус кампании не изменен"


def _error_detail(response: httpx.Response) -> str:
    """Prefer the API's JSON "detail" field; fall back to the raw body text."""
    try:
        body = response.json()
    except ValueError:  # JSONDecodeError and friends
        return response.text
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return response.text


def _is_no_change(response: httpx.Response) -> bool:
    """422 no-op discriminator for pause/start: either the spec's sentinel
    «Статус кампании не изменен» is in the body, or the body is text/plain
    (the form the spec documents for this response)."""
    if _NO_CHANGE_MARK in response.text:
        return True
    return response.headers.get("content-type", "").startswith("text/plain")


class WbClient:
    """Read-only WB advert-api client, rate limited per cabinet and method class.

    Args:
        token: WB personal API token (sent raw in the Authorization header).
        limiter: shared LimiterRegistry; buckets are keyed by (cabinet_id, class).
        http: optional pre-built httpx.AsyncClient (e.g. a respx/transport stub
            in tests); when omitted, one is created with base_url=HOST, 30s timeout.
        cabinet_id: key for the limiter registry; set to the WB cabinet's DB id.
        retry_sleep: injectable sleep for the single 429 retry (real asyncio.sleep
            in production, a spy in tests).
    """

    def __init__(
        self,
        token: str,
        limiter: LimiterRegistry,
        http: httpx.AsyncClient | None = None,
        *,
        cabinet_id: int = 0,
        retry_sleep: RetrySleep = asyncio.sleep,
    ) -> None:
        self._token = token
        self.limiter = limiter
        self.cabinet_id = cabinet_id
        self._retry_sleep = retry_sleep
        self._owns_http = http is None
        self.http = http if http is not None else httpx.AsyncClient(base_url=HOST, timeout=30)

    # -- request core ------------------------------------------------------

    async def _request(
        self,
        klass: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        response = await self._fetch(klass, method, path, params=params, json_body=json_body)
        if response.is_success:
            return response.json()
        raise WbApiError(response.status_code, _error_detail(response))

    async def _fetch(
        self,
        klass: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """One rate-limited request with the single 429 retry; returns the raw
        response so callers can inspect status/headers (pause/start 422)."""
        await self.limiter.bucket(self.cabinet_id, klass).acquire()
        headers = {"Authorization": self._token}
        try:
            response = await self.http.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.RequestError as exc:
            raise WbApiError(-1, repr(exc)) from exc

        if response.status_code == 429:
            response = await self._retry_429(method, path, params, json_body, headers, response)
        return response

    async def _retry_429(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        json_body: Any,
        headers: dict[str, str],
        response: httpx.Response,
    ) -> httpx.Response:
        """Wait retry_after seconds and resend exactly once; a second 429 raises."""
        raw = response.headers.get("X-Ratelimit-Retry")
        try:
            retry_after = float(raw) if raw is not None else _DEFAULT_RETRY_AFTER
        except ValueError:
            retry_after = _DEFAULT_RETRY_AFTER
        logger.warning("WB 429 on %s %s: retrying once after %.1fs", method, path, retry_after)
        await self._retry_sleep(retry_after)
        try:
            retried = await self.http.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.RequestError as exc:
            raise WbApiError(-1, repr(exc), retry_after) from exc
        if retried.status_code == 429:
            raise WbApiError(429, _error_detail(retried), retry_after)
        return retried

    # -- endpoints (thin wrappers, return parsed JSON) ----------------------

    async def ping(self) -> dict:
        """GET /ping — availability probe, limit class "count"."""
        return await self._request("count", "GET", "/ping")

    async def promotion_count(self) -> dict:
        """GET /adv/v1/promotion/count — campaign counters by type/status."""
        path = "/adv/v1/promotion/count"
        return await self._request(klass(path), "GET", path)

    async def adverts(self, ids: list[int]) -> list[dict]:
        """GET /api/advert/v2/adverts — campaign details, ids comma-joined (<=50)."""
        if len(ids) > _MAX_ADVERTS_IDS:
            logger.warning(
                "adverts(): %d ids requested, truncating to %d", len(ids), _MAX_ADVERTS_IDS
            )
            ids = ids[:_MAX_ADVERTS_IDS]
        path = "/api/advert/v2/adverts"
        return await self._request(
            klass(path), "GET", path, params={"ids": ",".join(map(str, ids))}
        )

    async def budgets(self, advert_ids: list[int]) -> list[dict]:
        """POST /api/advert/v2/budget — budgets (read-by-POST).

        Живой факт (спайк 13.10.2026, BYN-кабинет): поле тела — advertIds,
        не ids («advertIds must contain from 1 to 50 items»).
        """
        path = "/api/advert/v2/budget"
        return await self._request(
            klass(path), "POST", path, json_body={"advertIds": list(advert_ids)}
        )

    async def balance(self) -> dict:
        """GET /adv/v1/balance — cabinet balance."""
        path = "/adv/v1/balance"
        return await self._request(klass(path), "GET", path)

    async def fullstats(self, advert_ids: list[int], begin: date, end: date) -> list[dict]:
        """GET /adv/v3/fullstats — daily stats; dates are ISO YYYY-MM-DD."""
        path = "/adv/v3/fullstats"
        return await self._request(
            klass(path),
            "GET",
            path,
            params={
                "ids": ",".join(map(str, advert_ids)),
                "beginDate": begin.isoformat(),
                "endDate": end.isoformat(),
            },
        )

    async def bids_min(self, nm_ids: list[int]) -> dict:
        """POST /api/advert/v1/bids/min — minimal bids.

        Body format NOT confirmed by the spike — see .scratch/facts.md.
        """
        path = "/api/advert/v1/bids/min"
        return await self._request(
            klass(path), "POST", path, json_body={"nm_ids": list(nm_ids)}
        )

    async def recommendations(self, params: dict) -> dict:
        """GET /api/advert/v0/bids/recommendations — auction snapshot.

        Query parameters NOT confirmed by the spike — see .scratch/facts.md.
        """
        path = "/api/advert/v0/bids/recommendations"
        return await self._request(klass(path), "GET", path, params=params)

    async def normquery_list(self, advert_id: int) -> dict:
        """POST /adv/v0/normquery/list — cluster list of a campaign.

        Body format NOT confirmed by the spike — see .scratch/facts.md.
        """
        path = "/adv/v0/normquery/list"
        return await self._request(
            klass(path), "POST", path, json_body={"advert_id": advert_id}
        )

    async def normquery_stats(self, body: dict) -> dict:
        """POST /adv/v1/normquery/stats — cluster stats (body passed as-is)."""
        path = "/adv/v1/normquery/stats"
        return await self._request(klass(path), "POST", path, json_body=body)

    # -- write endpoints (phase 2 controller; live only after owner's go-ahead) --

    async def pause(self, advert_id: int) -> None:
        """GET /adv/v0/pause?id= — pause a running (status 9) campaign.

        200 (no body) -> None; 422 «Статус кампании не изменен» -> None too —
        the campaign is already paused, the expected no-op on retries (spec:
        text/plain 422); other non-2xx raise WbApiError. Limit class
        "control" (5/s personal).
        """
        await self._switch("/adv/v0/pause", advert_id)

    async def start(self, advert_id: int) -> None:
        """GET /adv/v0/start?id= — resume a paused (status 4/11) campaign.

        Same contract as pause(); a 400 "Low Budget" (not enough budget)
        surfaces as WbApiError — check the budget before starting (spec note).
        """
        await self._switch("/adv/v0/start", advert_id)

    async def _switch(self, path: str, advert_id: int) -> None:
        """Shared pause/start core: 200 and the 422 no-change no-op -> None."""
        response = await self._fetch(klass(path), "GET", path, params={"id": advert_id})
        if response.is_success:
            return  # 200 carries no body
        if response.status_code == 422 and _is_no_change(response):
            return  # already in the desired state — not an error
        raise WbApiError(response.status_code, _error_detail(response))

    async def daily_limits_get(self, advert_ids: list[int]) -> list[dict]:
        """GET /api/advert/v0/daily-limits?advertIds= — native daily limits.

        Живой факт (спайк-2 прод 14.10.2026): daily-limits существует ТОЛЬКО
        для cpc-кампаний (смешанный список даёт 400 «daily limit is not
        available for CPM campaigns») — caller passes cpc ids only. The
        {"adverts": [...]} wrapper is unwrapped and int/bool fields coerced;
        more than 100 ids are truncated (WB caps advertIds at 100).
        """
        if len(advert_ids) > _MAX_DAILY_LIMIT_IDS:
            logger.warning(
                "daily_limits_get(): %d ids requested, truncating to %d",
                len(advert_ids), _MAX_DAILY_LIMIT_IDS,
            )
            advert_ids = advert_ids[:_MAX_DAILY_LIMIT_IDS]
        path = "/api/advert/v0/daily-limits"
        payload = await self._request(
            klass(path), "GET", path, params={"advertIds": ",".join(map(str, advert_ids))}
        )
        return [
            {
                "advertId": int(item["advertId"]),
                "enabled": bool(item["enabled"]),
                "dailyLimit": int(item["dailyLimit"]),
                "spentToday": int(item["spentToday"]),
                "carryOverEnabled": bool(item["carryOverEnabled"]),
                "valid": bool(item["valid"]),
                "requiredLimit": int(item["requiredLimit"]),
            }
            for item in (payload.get("adverts") or [])
        ]

    async def daily_limits_put(
        self,
        advert_ids: list[int],
        enabled: bool,
        daily_limit_kopecks: int | None = None,
        carry_over: bool | None = None,
    ) -> dict:
        """PUT /api/advert/v0/daily-limits — ONE limit for the whole batch.

        Body {"advertIds": [...], "enabled": bool[, "dailyLimit": int kopecks,
        "carryOverEnabled": bool]} — dailyLimit/carryOverEnabled are sent only
        when given (switching off is just {"advertIds", "enabled": false}).
        Personal/service tokens only (spec: x-token-types)."""
        path = "/api/advert/v0/daily-limits"
        body: dict[str, Any] = {"advertIds": list(advert_ids), "enabled": enabled}
        if daily_limit_kopecks is not None:
            body["dailyLimit"] = int(daily_limit_kopecks)
        if carry_over is not None:
            body["carryOverEnabled"] = carry_over
        return await self._request(klass(path), "PUT", path, json_body=body)

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying client, but only if WbClient created it."""
        if self._owns_http:
            await self.http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
