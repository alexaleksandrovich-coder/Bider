"""Thin async client for the WB statistics-api (supplier/orders).

A separate client from WbClient because both the host
(statistics-api.wildberries.ru) and the rate limits differ:
/api/v1/supplier/orders is 1 request/min with a burst of 10 (live fact,
spike-2 prod 14.10.2026: X-Ratelimit-Remaining=9 / Limit=10 right after one
request) — limit class "orders" in app.wb.constants. The request core (raw
``Authorization: <token>`` with no Bearer prefix, exactly one 429 retry after
X-Ratelimit-Retry seconds, WbApiError mapping) is inherited from WbClient
unchanged. Timestamps are Moscow time (UTC+3, no DST); dateFrom is sent as
RFC3339 "YYYY-MM-DDTHH:MM:SS+03:00".
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.wb.client import RetrySleep, WbClient
from app.wb.constants import STATISTICS_HOST, klass
from app.wb.limiter import LimiterRegistry

_MSK = timezone(timedelta(hours=3))  # statistics-api: Moscow, UTC+3, no DST

ORDERS_PATH = "/api/v1/supplier/orders"


def to_msk_rfc3339(moment: datetime) -> str:
    """Format a datetime as the RFC3339 MSK string WB statistics-api expects.

    Aware datetimes are converted to UTC+03; naive ones are taken AS MSK —
    живой факт: lastChangeDate приходит без таймзоны ("2026-10-12T21:21:39").
    Microseconds are dropped (the API takes second resolution).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_MSK)
    return moment.astimezone(_MSK).replace(microsecond=0).isoformat()


class StatisticsClient(WbClient):
    """Read-only WB statistics-api client (limit class "orders": 1/min, burst 10).

    Args and behavior mirror WbClient (see app/wb/client.py); only the base
    URL and the endpoint set differ.
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
        if http is None:
            http = httpx.AsyncClient(base_url=STATISTICS_HOST, timeout=30)
        super().__init__(token, limiter, http, cabinet_id=cabinet_id, retry_sleep=retry_sleep)

    # -- endpoints -----------------------------------------------------------

    async def orders(self, date_from: datetime) -> list[dict]:
        """GET /api/v1/supplier/orders — order rows with lastChangeDate >= dateFrom.

        flag=0 (increment by lastChangeDate, up to ~80000 rows per response);
        an empty response means everything is fetched. Data is kept by WB for
        90 days and refreshed every 30 minutes.
        """
        return await self._request(
            klass(ORDERS_PATH),
            "GET",
            ORDERS_PATH,
            params={"dateFrom": to_msk_rfc3339(date_from), "flag": 0},
        )
