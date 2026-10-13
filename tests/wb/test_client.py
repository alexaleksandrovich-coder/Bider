"""Tests for WbClient over respx: no real network, fake limiter time."""

import logging
from datetime import date

import httpx
import pytest
import respx

from app.wb.client import WbClient
from app.wb.constants import HOST, klass
from app.wb.errors import WbApiError
from app.wb.limiter import LimiterRegistry
from tests.wb.conftest import FakeClock, SpySleep

TOKEN = "test-token"


def make_client(retry_sleep: SpySleep | None = None) -> WbClient:
    """A client with a fake-time limiter (buckets never really sleep) that owns
    its own httpx.AsyncClient (as in production) intercepted by respx."""
    return WbClient(
        TOKEN,
        LimiterRegistry(clock=FakeClock(), sleep=SpySleep()),
        retry_sleep=retry_sleep if retry_sleep is not None else SpySleep(),
    )


@respx.mock
async def test_ping_returns_json_and_sends_raw_token():
    route = respx.get(f"{HOST}/ping").respond(200, json={"ok": True})
    async with make_client() as client:
        result = await client.ping()
    assert result == {"ok": True}
    assert route.call_count == 1
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == TOKEN
    assert "bearer" not in auth.lower()


@respx.mock
async def test_429_with_retry_header_sleeps_once_and_retries():
    route = respx.get(f"{HOST}/ping")
    route.side_effect = [
        httpx.Response(429, headers={"X-Ratelimit-Retry": "2"}),
        httpx.Response(200, json={"ok": True}),
    ]
    retry_spy = SpySleep()
    async with make_client(retry_sleep=retry_spy) as client:
        result = await client.ping()
    assert result == {"ok": True}
    assert route.call_count == 2
    assert retry_spy.calls == [2.0]  # exactly one sleep, of exactly 2s


@respx.mock
async def test_429_without_retry_header_defaults_to_five_seconds():
    route = respx.get(f"{HOST}/ping")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"ok": True}),
    ]
    retry_spy = SpySleep()
    async with make_client(retry_sleep=retry_spy) as client:
        assert await client.ping() == {"ok": True}
    assert retry_spy.calls == [5.0]
    assert route.call_count == 2


@respx.mock
async def test_second_429_raises_wb_api_error():
    route = respx.get(f"{HOST}/ping")
    route.side_effect = [
        httpx.Response(429, headers={"X-Ratelimit-Retry": "2"}),
        httpx.Response(429, headers={"X-Ratelimit-Retry": "2"}, json={"detail": "rate limited"}),
    ]
    retry_spy = SpySleep()
    async with make_client(retry_sleep=retry_spy) as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.ping()
    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 2.0
    assert retry_spy.calls == [2.0]  # still only one retry sleep
    assert route.call_count == 2


@respx.mock
async def test_401_scope_error_maps_to_wb_api_error():
    route = respx.post(f"{HOST}/api/advert/v2/budget").respond(
        401, json={"detail": "scope is not allowed for this resource", "origin": "ag-advert"}
    )
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.budgets([111, 222])
    err = excinfo.value
    assert err.status == 401
    assert "scope" in err.detail
    assert err.retry_after is None
    assert "401" in str(err)
    assert route.calls.last.request.headers["Authorization"] == TOKEN  # POST too


@respx.mock
async def test_network_error_maps_to_wb_api_error_minus_one():
    respx.get(f"{HOST}/adv/v1/balance").mock(side_effect=httpx.ConnectError("boom"))
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.balance()
    assert excinfo.value.status == -1
    assert "boom" in excinfo.value.detail


@respx.mock
async def test_adverts_truncates_ids_over_50_and_warns(caplog):
    route = respx.get(f"{HOST}/api/advert/v2/adverts").respond(200, json=[])
    async with make_client() as client:
        with caplog.at_level(logging.WARNING, logger="app.wb.client"):
            await client.adverts(list(range(1, 61)))  # 60 ids -> cut to 50
    parts = route.calls.last.request.url.params["ids"].split(",")
    assert len(parts) == 50
    assert parts[0] == "1" and parts[-1] == "50"  # the first 50 kept
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


@respx.mock
async def test_fullstats_sends_dates_as_iso_and_ids_comma_joined():
    route = respx.get(f"{HOST}/adv/v3/fullstats").respond(200, json=[])
    async with make_client() as client:
        await client.fullstats([1, 2], date(2026, 10, 1), date(2026, 10, 13))
    params = route.calls.last.request.url.params
    assert params["ids"] == "1,2"
    assert params["beginDate"] == "2026-10-01"
    assert params["endDate"] == "2026-10-13"


def test_unknown_path_rejected_by_klass_mapper():
    with pytest.raises(ValueError):
        klass("/api/adv/definitely/unknown")
