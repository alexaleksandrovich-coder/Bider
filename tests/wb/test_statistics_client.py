"""Tests for StatisticsClient over respx: no real network, fake limiter time.

Живые факты (спайк-2, прод 14.10.2026, «Улов»): GET /api/v1/supplier/orders на
хосте statistics-api.wildberries.ru; Authorization — сырой токен БЕЗ Bearer;
dateFrom — RFC3339 в МСК (UTC+3); flag=0; лимит 1/мин со всплеском 10
(remaining=9 limit=10 сразу после одного запроса) → класс "orders": (1, 60).
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.wb.constants import LIMITS, STATISTICS_HOST, klass
from app.wb.errors import WbApiError
from app.wb.limiter import LimiterRegistry
from app.wb.statistics_client import StatisticsClient
from tests.wb.conftest import FakeClock, SpySleep

TOKEN = "stats-token"
_ORDERS_URL = f"{STATISTICS_HOST}/api/v1/supplier/orders"


def make_client(retry_sleep: SpySleep | None = None) -> StatisticsClient:
    """A client with a fake-time limiter (buckets never really sleep) that owns
    its own httpx.AsyncClient (as in production) intercepted by respx."""
    return StatisticsClient(
        TOKEN,
        LimiterRegistry(clock=FakeClock(), sleep=SpySleep()),
        retry_sleep=retry_sleep if retry_sleep is not None else SpySleep(),
    )


def test_orders_limit_class_and_path_mapped():
    """Живой факт: 1/мин, всплеск 10; путь находит класс в едином klass()."""
    assert LIMITS["orders"] == (1, 60)
    assert klass("/api/v1/supplier/orders") == "orders"


@respx.mock
async def test_orders_returns_json_sends_raw_token_and_msk_rfc3339():
    route = respx.get(_ORDERS_URL).respond(200, json=[{"srid": "ei.iabc.0.0"}])
    async with make_client() as client:
        result = await client.orders(datetime(2026, 10, 12, 18, 21, 39, 123456, tzinfo=UTC))
    assert result == [{"srid": "ei.iabc.0.0"}]
    assert route.call_count == 1
    params = route.calls.last.request.url.params
    # UTC 18:21:39 -> МСК 21:21:39; микросекунды отброшены; RFC3339 c +03:00
    assert params["dateFrom"] == "2026-10-12T21:21:39+03:00"
    assert params["flag"] == "0"
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == TOKEN
    assert "bearer" not in auth.lower()


@respx.mock
async def test_orders_naive_datetime_is_treated_as_msk():
    """Живой факт: lastChangeDate приходит БЕЗ таймзоны — наивное время = МСК."""
    route = respx.get(_ORDERS_URL).respond(200, json=[])
    async with make_client() as client:
        await client.orders(datetime(2026, 10, 12, 21, 21, 39))
    assert route.calls.last.request.url.params["dateFrom"] == "2026-10-12T21:21:39+03:00"


@respx.mock
async def test_429_with_retry_header_sleeps_once_and_retries():
    route = respx.get(_ORDERS_URL)
    route.side_effect = [
        httpx.Response(429, headers={"X-Ratelimit-Retry": "2"}),
        httpx.Response(200, json=[]),
    ]
    retry_spy = SpySleep()
    async with make_client(retry_sleep=retry_spy) as client:
        assert await client.orders(datetime(2026, 10, 12, tzinfo=UTC)) == []
    assert route.call_count == 2  # ровно один ретрай, как в WbClient
    assert retry_spy.calls == [2.0]


@respx.mock
async def test_second_429_raises_wb_api_error():
    route = respx.get(_ORDERS_URL)
    route.side_effect = [
        httpx.Response(429, headers={"X-Ratelimit-Retry": "2"}),
        httpx.Response(429, json={"detail": "rate limited"}),
    ]
    retry_spy = SpySleep()
    async with make_client(retry_sleep=retry_spy) as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.orders(datetime(2026, 10, 12, tzinfo=UTC))
    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 2.0
    assert retry_spy.calls == [2.0]  # still only one retry sleep


@respx.mock
async def test_403_scope_error_maps_to_wb_api_error():
    """Живой факт (спайк-2): 403 {"detail": "scope ...", "origin": "ag-statistics"}."""
    route = respx.get(_ORDERS_URL).respond(
        403, json={"detail": "scope is not allowed for this resource", "origin": "ag-statistics"}
    )
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.orders(datetime(2026, 10, 12, tzinfo=UTC))
    err = excinfo.value
    assert err.status == 403
    assert "scope" in err.detail
    assert err.retry_after is None
    assert "403" in str(err)
    assert route.calls.last.request.headers["Authorization"] == TOKEN


@respx.mock
async def test_network_error_maps_to_wb_api_error_minus_one():
    respx.get(_ORDERS_URL).mock(side_effect=httpx.ConnectError("boom"))
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.orders(datetime(2026, 10, 12, tzinfo=UTC))
    assert excinfo.value.status == -1
    assert "boom" in excinfo.value.detail
