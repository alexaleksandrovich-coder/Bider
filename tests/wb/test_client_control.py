"""Client control-method tests (pause/start/daily-limits) over respx.

Wire forms from .scratch/facts-phase2.md (spec snapshot + prod probe):
pause/start — GET /adv/v0/pause|start?id=<int>, 200 without a body,
422 text/plain «Статус кампании не изменен» is an EXPECTED no-op (not an
error), start can answer 400 LowBudget; daily-limits — GET query advertIds
(csv, <=100 ids), response wrapped in {"adverts": [...]}, PUT takes ONE
{"advertIds": [...], "enabled": bool[, "dailyLimit", "carryOverEnabled"]}
object for the whole batch. Live fact: daily-limits exists ONLY for cpc
(400 on CPM) — that is the caller's concern, the client just maps errors.
"""

import json

import httpx
import pytest
import respx

from app.wb.client import WbClient
from app.wb.constants import HOST, klass
from app.wb.errors import WbApiError
from app.wb.limiter import LimiterRegistry
from tests.wb.conftest import FakeClock, SpySleep

TOKEN = "test-token"
_NO_CHANGE = "Статус кампании не изменен"


def make_client() -> WbClient:
    """A client with a fake-time limiter (buckets never really sleep)."""
    return WbClient(TOKEN, LimiterRegistry(clock=FakeClock(), sleep=SpySleep()))


def test_control_paths_have_limit_classes():
    assert klass("/adv/v0/pause") == "control"
    assert klass("/adv/v0/start") == "control"
    assert klass("/api/advert/v0/daily-limits") == "daily_limits"


@respx.mock
async def test_pause_200_returns_none():
    route = respx.get(f"{HOST}/adv/v0/pause").respond(200)  # 200 has NO body
    async with make_client() as client:
        assert await client.pause(12345) is None
    assert route.call_count == 1
    assert route.calls.last.request.url.params["id"] == "12345"
    assert route.calls.last.request.headers["Authorization"] == TOKEN


@respx.mock
async def test_start_200_returns_none():
    route = respx.get(f"{HOST}/adv/v0/start").respond(200)
    async with make_client() as client:
        assert await client.start(12345) is None
    assert route.calls.last.request.url.params["id"] == "12345"


@respx.mock
async def test_pause_422_status_no_change_is_noop():
    """422 text/plain «Статус кампании не изменен» — expected no-op, not an error."""
    route = respx.get(f"{HOST}/adv/v0/pause")
    route.side_effect = [httpx.Response(422, text=_NO_CHANGE)]
    async with make_client() as client:
        assert await client.pause(12345) is None
    assert route.call_count == 1


@respx.mock
async def test_pause_422_other_payload_is_an_error():
    """A 422 that is NOT the no-change no-op (json body, other text) raises."""
    route = respx.get(f"{HOST}/adv/v0/pause")
    route.side_effect = [httpx.Response(422, json={"detail": "что-то другое"})]
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.pause(12345)
    assert excinfo.value.status == 422
    assert "что-то другое" in excinfo.value.detail


@respx.mock
async def test_start_400_low_budget_raises_wb_api_error():
    respx.get(f"{HOST}/adv/v0/start").respond(
        400, json={"error": "AdvertChangeStatus: Low Budget: not enough budget"}
    )
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.start(12345)
    assert excinfo.value.status == 400
    assert "Low Budget" in excinfo.value.detail


@respx.mock
async def test_pause_401_still_raises():
    respx.get(f"{HOST}/adv/v0/pause").respond(
        401, json={"detail": "scope is not allowed for this resource"}
    )
    async with make_client() as client:
        with pytest.raises(WbApiError) as excinfo:
            await client.pause(12345)
    assert excinfo.value.status == 401


_LIMIT_ROW = {
    "advertId": 111,
    "enabled": True,
    "dailyLimit": 150000,
    "spentToday": 60000,
    "currency": "BYN",
    "carryOverEnabled": False,
    "valid": True,
    "requiredLimit": 100000,
}


@respx.mock
async def test_daily_limits_get_unwraps_adverts_wrapper():
    route = respx.get(f"{HOST}/api/advert/v0/daily-limits").respond(
        200, json={"adverts": [_LIMIT_ROW]}
    )
    async with make_client() as client:
        result = await client.daily_limits_get([111, 222])
    assert route.calls.last.request.url.params["advertIds"] == "111,222"
    assert result == [
        {
            "advertId": 111,
            "enabled": True,
            "dailyLimit": 150000,
            "spentToday": 60000,
            "carryOverEnabled": False,
            "valid": True,
            "requiredLimit": 100000,
        }
    ]


@respx.mock
async def test_daily_limits_get_truncates_over_100_ids(caplog):
    route = respx.get(f"{HOST}/api/advert/v0/daily-limits").respond(200, json={"adverts": []})
    async with make_client() as client:
        with caplog.at_level("WARNING", logger="app.wb.client"):
            await client.daily_limits_get(list(range(1, 122)))  # 121 ids -> cut to 100
    parts = route.calls.last.request.url.params["advertIds"].split(",")
    assert len(parts) == 100
    assert parts[-1] == "100"
    assert any(rec.levelno >= 30 for rec in caplog.records)


@respx.mock
async def test_daily_limits_put_body_fields_only_when_given():
    route = respx.put(f"{HOST}/api/advert/v0/daily-limits").respond(
        200, json={"adverts": [{"advertId": 3, "belowMinLimit": False, "requiredLimit": 100000}]}
    )
    async with make_client() as client:
        result = await client.daily_limits_put([1, 2], False)
        body = json.loads(route.calls.last.request.content)
        assert body == {"advertIds": [1, 2], "enabled": False}  # switch-off: no limit fields

        result2 = await client.daily_limits_put(
            [3], True, daily_limit_kopecks=150000, carry_over=True
        )
        body2 = json.loads(route.calls.last.request.content)
        assert body2 == {
            "advertIds": [3],
            "enabled": True,
            "dailyLimit": 150000,
            "carryOverEnabled": True,
        }

    assert result == {"adverts": [{"advertId": 3, "belowMinLimit": False, "requiredLimit": 100000}]}
    assert result2 == result
