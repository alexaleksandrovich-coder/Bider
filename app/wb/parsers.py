"""Pure parsers: WB advert-api payloads -> plain rows for app.repos.

No IO: every function takes already-decoded JSON and returns dicts.

Field naming: the spike got 403 on all advert-api methods (tokens lacked the
"Продвижение" scope, see .scratch/facts.md), so fixtures follow the OpenAPI
snapshot from 01-АНАЛИЗ-биддеров-WB.md. Where camelCase vs snake_case could
differ between API generations, values are read via get_any(d, *names), which
accepts BOTH spellings — сверить после получения токенов и повторного спайка.

Money: values are stored as-is (int), assumed to be kopecks of the cabinet's
base currency. The kopecks scale is NOT yet confirmed by the spike (facts.md
lists currency/cpcStep/cpmStep as blocked) — re-check against .scratch/facts.md
once live 2xx responses arrive, before trusting any formatted output.
"""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

_PLACEMENTS = ("search", "recommendations")


def get_any(d: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """Return the first non-None value among `names` (camelCase/snake_case twins).

    Used because the live field naming is unconfirmed (spike 403, facts.md):
    читаем оба варианта — сверить после получения токенов.
    """
    for name in names:
        value = d.get(name)
        if value is not None:
            return value
    return default


def _to_int(value: Any) -> int:
    """Missing/None numeric fields count as 0; numeric strings are tolerated."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_datetime(value: Any) -> datetime | None:
    """ISO datetime parse, tolerate None/unparsable -> None."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    """Date from "YYYY-MM-DD", tolerate ISO datetime ("...T00:00:00Z") -> date."""
    if value is None:
        return None
    text = str(value)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_adverts(payload: list[dict]) -> list[dict]:
    """Parse GET /api/advert/v2/adverts into campaign rows (bids flattened).

    Row: {advert_id, name, type, status, payment_type, bid_type, search,
    recommendations, change_time: datetime|None, bids: [{nm_id, placement,
    bid_kopecks}]}. Placements come from settings.placements or the top level
    (both spellings accepted — сверить после получения токенов). bids_kopecks
    "combined" (unified bid) is deliberately ignored: BidRow / bid_history
    store only search/recommendations placements. Missing or zero bids are
    not recorded.
    """
    rows: list[dict] = []
    for camp in payload:
        placements = get_any(camp, "placements")
        if placements is None:
            settings_obj = get_any(camp, "settings")
            if isinstance(settings_obj, Mapping):
                placements = get_any(settings_obj, "placements")
        if not isinstance(placements, Mapping):
            placements = {}

        bids: list[dict] = []
        for nm in get_any(camp, "nmSettings", "nm_settings", default=()):
            nm_id = _to_int(get_any(nm, "nmID", "nmId", "nm_id"))
            bids_map = get_any(nm, "bidsKopecks", "bids_kopecks", default={})
            if not isinstance(bids_map, Mapping):
                continue
            for placement in _PLACEMENTS:
                bid = bids_map.get(placement)
                if bid:  # absent and zero bids are not recorded
                    bids.append(
                        {"nm_id": nm_id, "placement": placement, "bid_kopecks": _to_int(bid)}
                    )

        rows.append(
            {
                "advert_id": _to_int(get_any(camp, "advertId", "advert_id")),
                "name": str(get_any(camp, "name", default="")),
                "type": _to_int(get_any(camp, "type")),
                "status": _to_int(get_any(camp, "status")),
                "payment_type": str(get_any(camp, "paymentType", "payment_type", default="")),
                "bid_type": str(get_any(camp, "bidType", "bid_type", default="")),
                "search": bool(placements.get("search")),
                "recommendations": bool(placements.get("recommendations")),
                "change_time": _to_datetime(
                    get_any(camp, "changeTime", "changed_time", "change_time")
                ),
                "bids": bids,
            }
        )
    return rows


def parse_fullstats(payload: list[dict]) -> tuple[list[dict], list[dict]]:
    """Parse GET /adv/v3/fullstats into (stat_rows, position_rows).

    stat_rows: {advert_id, nm_id, date: date, app_type, views, clicks, atbs,
    orders, shks, sum_kopecks, sum_price_kopecks, cpc_kopecks, canceled}.
    days[] may repeat a (date, appType) pair — entries are summed per
    (advert_id, nm_id, date, app_type). cpc_kopecks = sum_kopecks // clicks
    when clicks > 0, else 0. Day-level totals are ignored on purpose: stats
    are stored per nm (the stats PK includes nm_id); WB returns the nms[]
    breakdown. position_rows from boosterStats[]: {advert_id, nm_id, date,
    avg_position: float}, advert_id taken from the top-level record.
    """
    agg: dict[tuple[Any, ...], dict] = {}
    positions: dict[tuple[Any, ...], dict] = {}

    for camp in payload:
        advert_id = _to_int(get_any(camp, "advertId", "advert_id"))

        for day in get_any(camp, "days", default=()):
            stat_date = _to_date(get_any(day, "date"))
            if stat_date is None:
                continue  # no key to aggregate on
            app_type = str(get_any(day, "appType", "app_type", default=""))
            for nm in get_any(day, "nms", default=()):
                nm_id = _to_int(get_any(nm, "nmID", "nmId", "nm_id"))
                key = (advert_id, nm_id, stat_date, app_type)
                row = agg.setdefault(
                    key,
                    {
                        "advert_id": advert_id,
                        "nm_id": nm_id,
                        "date": stat_date,
                        "app_type": app_type,
                        "views": 0,
                        "clicks": 0,
                        "atbs": 0,
                        "orders": 0,
                        "shks": 0,
                        "sum_kopecks": 0,
                        "sum_price_kopecks": 0,
                        "canceled": 0,
                    },
                )
                # repeated (date, appType) entries accumulate
                row["views"] += _to_int(get_any(nm, "views"))
                row["clicks"] += _to_int(get_any(nm, "clicks"))
                row["atbs"] += _to_int(get_any(nm, "atbs"))
                row["orders"] += _to_int(get_any(nm, "orders"))
                row["shks"] += _to_int(get_any(nm, "shks"))
                row["canceled"] += _to_int(get_any(nm, "canceled"))
                # money stored as-is (assumed kopecks — see module docstring)
                row["sum_kopecks"] += _to_int(get_any(nm, "sum"))
                row["sum_price_kopecks"] += _to_int(get_any(nm, "sum_price", "sumPrice"))

        for booster in get_any(camp, "boosterStats", "booster_stats", default=()):
            stat_date = _to_date(get_any(booster, "date"))
            if stat_date is None:
                continue
            nm_id = _to_int(get_any(booster, "nmID", "nmId", "nm", "nm_id"))
            # avgPosition/avg_position — naming not confirmed by the spike
            key = (advert_id, nm_id, stat_date)
            positions[key] = {
                "advert_id": advert_id,
                "nm_id": nm_id,
                "date": stat_date,
                "avg_position": float(get_any(booster, "avgPosition", "avg_position", default=0.0)),
            }

    stat_rows = sorted(
        agg.values(), key=lambda r: (r["advert_id"], r["nm_id"], r["date"], r["app_type"])
    )
    for row in stat_rows:
        row["cpc_kopecks"] = row["sum_kopecks"] // row["clicks"] if row["clicks"] > 0 else 0

    position_rows = sorted(
        positions.values(), key=lambda r: (r["advert_id"], r["nm_id"], r["date"])
    )
    return stat_rows, position_rows


def parse_budget(payload: list[dict]) -> list[dict]:
    """Parse POST /api/advert/v2/budget into rows {advert_id, cash, netting, total}.

    Field names may be camelCase (payment-type dependent) — normalized via
    get_any. Money values are kept as-is (int, assumed kopecks).
    """
    rows: list[dict] = []
    for item in payload:
        rows.append(
            {
                "advert_id": _to_int(get_any(item, "advertId", "advert_id")),
                "cash": _to_int(get_any(item, "cash")),
                "netting": _to_int(get_any(item, "netting")),
                "total": _to_int(get_any(item, "total")),
            }
        )
    return rows
