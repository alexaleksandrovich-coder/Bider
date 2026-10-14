"""Parser tests against WB-spec fixtures.

Live WB data is not available yet (the spike got 403 on every method — see
.scratch/facts.md), so fixtures in tests/fixtures/ follow the OpenAPI snapshot
from 01-АНАЛИЗ-биддеров-WB.md. Expected rows below are hand-computed from the
fixtures and hardcoded; where camelCase/snake_case naming could differ the
parsers must accept BOTH spellings (сверить после получения токенов).
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

from app.wb.parsers import parse_adverts, parse_budget, parse_fullstats

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_parse_adverts_fixture():
    rows = parse_adverts(_load("adverts_sample.json"))
    assert len(rows) == 3

    r1, r2, r3 = rows
    # placements nested in settings.placements
    assert r1 == {
        "advert_id": 1001,
        "name": "Поиск-1",
        "type": 8,
        "status": 9,
        "payment_type": "cpm",
        "bid_type": "manual",
        "search": True,
        "recommendations": False,
        "change_time": datetime.fromisoformat("2026-10-09T12:30:00+03:00"),
        "bids": [{"nm_id": 111, "placement": "search", "bid_kopecks": 25000}],
    }
    # unified (cpc) campaign: no search/recommendations placements, null changeTime
    assert r2["advert_id"] == 1002
    assert r2["payment_type"] == "cpc"
    assert r2["search"] is False
    assert r2["recommendations"] is False
    assert r2["change_time"] is None
    # "combined" (unified bid 900) must NOT appear in bids: BidRow stores
    # only search/recommendations placements.
    assert r2["bids"] == []
    # placements on the top level, both on
    assert r3["advert_id"] == 1003
    assert r3["search"] is True
    assert r3["recommendations"] is True
    assert r3["change_time"] is None
    assert r3["bids"] == [
        {"nm_id": 333, "placement": "search", "bid_kopecks": 21000},
        {"nm_id": 333, "placement": "recommendations", "bid_kopecks": 15000},
    ]


def test_parse_adverts_accepts_snake_case():
    payload = [
        {
            "advert_id": 7,
            "name": "Снейк",
            "type": 8,
            "status": 4,
            "payment_type": "cpc",
            "bid_type": "manual",
            "change_time": "2026-10-01T00:00:00Z",
            "placements": {"search": 1, "recommendations": 0},
            "nm_settings": [
                {"nm_id": 42, "bids_kopecks": {"search": 150, "recommendations": 0}}
            ],
        }
    ]
    rows = parse_adverts(payload)
    assert rows[0]["advert_id"] == 7
    assert rows[0]["payment_type"] == "cpc"
    assert rows[0]["search"] is True
    assert rows[0]["recommendations"] is False
    assert rows[0]["change_time"] == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    # zero/missing bids are not recorded
    assert rows[0]["bids"] == [{"nm_id": 42, "placement": "search", "bid_kopecks": 150}]


def test_parse_fullstats_fixture():
    stat_rows, position_rows = parse_fullstats(_load("fullstats_sample.json"))

    # two days[] entries on (2026-10-10, web) merge into one row; plus
    # android/web on 10.10 and web rows for nm 111+222 on 10.11 -> 4 rows
    assert len(stat_rows) == 4
    web10 = next(
        r
        for r in stat_rows
        if r["date"] == date(2026, 10, 10) and r["app_type"] == "web" and r["nm_id"] == 111
    )
    assert web10 == {
        "advert_id": 1,
        "nm_id": 111,
        "date": date(2026, 10, 10),
        "app_type": "web",
        "views": 100,  # 60 + 40 (aggregated over two entries)
        "clicks": 5,
        "atbs": 2,
        "orders": 1,
        "shks": 1,
        "sum_kopecks": 2500,
        "sum_price_kopecks": 50000,
        "canceled": 0,
        "cpc_kopecks": 500,  # 2500 // 5
    }

    android10 = next(r for r in stat_rows if r["app_type"] == "android")
    assert android10 == {
        "advert_id": 1,
        "nm_id": 111,
        "date": date(2026, 10, 10),
        "app_type": "android",
        "views": 50,
        "clicks": 2,
        "atbs": 1,
        "orders": 1,
        "shks": 0,
        "sum_kopecks": 1500,
        "sum_price_kopecks": 30000,
        "canceled": 0,
        "cpc_kopecks": 750,  # 1500 // 2
    }

    web11 = next(r for r in stat_rows if r["date"] == date(2026, 10, 11) and r["nm_id"] == 111)
    assert web11 == {
        "advert_id": 1,
        "nm_id": 111,
        "date": date(2026, 10, 11),
        "app_type": "web",
        "views": 80,
        "clicks": 4,
        "atbs": 3,
        "orders": 2,
        "shks": 2,
        "sum_kopecks": 2000,
        "sum_price_kopecks": 40000,
        "canceled": 1,
        "cpc_kopecks": 500,
    }

    nm222 = next(r for r in stat_rows if r["nm_id"] == 222)
    assert nm222 == {
        "advert_id": 1,
        "nm_id": 222,
        "date": date(2026, 10, 11),
        "app_type": "web",
        "views": 30,
        "clicks": 1,
        "atbs": 1,
        "orders": 1,
        "shks": 1,
        "sum_kopecks": 800,
        "sum_price_kopecks": 10000,
        "canceled": 0,
        "cpc_kopecks": 800,
    }

    assert len(position_rows) == 2
    assert position_rows[0] == {
        "advert_id": 1,
        "nm_id": 111,
        "date": date(2026, 10, 10),
        "avg_position": 3.4,
    }
    assert position_rows[1] == {
        "advert_id": 1,
        "nm_id": 222,
        "date": date(2026, 10, 10),
        "avg_position": 5.7,
    }


def test_parse_fullstats_tolerates_datetime_dates_and_missing_fields():
    payload = [
        {
            "advertId": 2,
            "days": [
                # ISO datetime instead of a plain date; numeric fields missing -> 0;
                # unknown extra fields are ignored
                {
                    "date": "2026-10-12T00:00:00+03:00",
                    "appType": "web",
                    "foo": "unknown",
                    "nms": [{"nmID": 5, "views": 10, "bar": [1]}],
                },
                {"date": "2026-10-12", "appType": "web", "nms": [{"nm_id": 5, "views": "15"}]},
            ],
            "boosterStats": [{"nm": 5, "date": "2026-10-12", "avgPosition": 2.5}],
        }
    ]
    stat_rows, position_rows = parse_fullstats(payload)
    assert stat_rows == [
        {
            "advert_id": 2,
            "nm_id": 5,
            "date": date(2026, 10, 12),
            "app_type": "web",
            "views": 25,
            "clicks": 0,
            "atbs": 0,
            "orders": 0,
            "shks": 0,
            "sum_kopecks": 0,
            "sum_price_kopecks": 0,
            "canceled": 0,
            "cpc_kopecks": 0,  # clicks == 0 -> cpc 0, no ZeroDivisionError
        }
    ]
    assert position_rows == [
        {"advert_id": 2, "nm_id": 5, "date": date(2026, 10, 12), "avg_position": 2.5}
    ]


def test_parse_budget_fixture():
    # money is converted to kopecks (*100) — live fact 14.10.2026
    assert parse_budget(_load("budget_sample.json")) == [
        {"advert_id": 1, "cash": 10000000, "netting": 5000000, "total": 15000000}
    ]


def test_parse_budget_snake_case_and_missing_fields():
    assert parse_budget([{"advert_id": 9}]) == [
        {"advert_id": 9, "cash": 0, "netting": 0, "total": 0}
    ]


def test_parse_budget_live_wrapped_shape():
    """Живая форма (спайк 14.10.2026): {"adverts": [{advertId, currency, total}]},
    total в целых единицах валюты (33 = 33 BYN), cash/netting отсутствуют."""
    payload = {
        "adverts": [
            {"advertId": 40908103, "currency": "BYN", "total": 33},
            {"advertId": 40737767, "currency": "BYN", "total": 15},
        ]
    }
    assert parse_budget(payload) == [
        {"advert_id": 40908103, "cash": 0, "netting": 0, "total": 3300},
        {"advert_id": 40737767, "cash": 0, "netting": 0, "total": 1500},
    ]


def test_parsers_empty_payloads():
    assert parse_adverts([]) == []
    assert parse_fullstats([]) == ([], [])
    assert parse_budget([]) == []


def test_parsers_tolerate_json_null():
    """Живой факт: WB отдаёт literal null вместо пустого списка за окно без данных."""
    assert parse_adverts(None) == []
    assert parse_fullstats(None) == ([], [])
    assert parse_budget(None) == []


def test_parse_adverts_live_wrapped_shape():
    """Живая форма (спайк 13.10.2026): {"adverts": [...]}, id/settings.name/
    settings.payment_type/timestamps.updated, type приходит из count (types)."""
    rows = parse_adverts(_load("adverts_live_sample.json"), types={20902648: 9})
    assert len(rows) == 1
    r = rows[0]
    assert r["advert_id"] == 20902648
    assert r["name"] == "Кампания от 25.10.2024"
    assert r["type"] == 9
    assert r["status"] == 7
    assert r["payment_type"] == "cpm"
    assert r["bid_type"] == "manual"
    assert r["search"] is True
    assert r["recommendations"] is False
    assert r["change_time"] == datetime.fromisoformat("2025-08-04T11:07:04.873268+03:00")
    # нулевая ставка recommendations не пишется
    assert r["bids"] == [{"nm_id": 273461001, "placement": "search", "bid_kopecks": 21500}]


def test_parse_adverts_live_wrapped_without_types():
    """Без types type = 0 (неизвестный), а не падение."""
    rows = parse_adverts(_load("adverts_live_sample.json"))
    assert rows[0]["type"] == 0


def test_parse_adverts_count_legacy_and_live_ids():
    from app.collector.runner import _advert_ids_and_types_from_count

    legacy = {"adverts": [{"type": 8, "chards": [{"status": 9, "advertList": [1, 2]}]}]}
    live = {
        "adverts": [
            {"type": 9, "status": 11, "advert_list": [{"advertId": 100, "changeTime": "x"}]}
        ]
    }
    assert _advert_ids_and_types_from_count(legacy) == ([1, 2], {1: 8, 2: 8})
    assert _advert_ids_and_types_from_count(live) == ([100], {100: 9})
