"""Presentation helpers: money/DRR formatting, relative time, WB dictionaries.

Pure functions, no IO — safe to use from templates (registered as Jinja
globals in app/web/pages.py) and from queries/tests.
"""

from datetime import UTC, datetime

CURRENCIES = ("BYN", "RUB")

# Словарь по спеке, сверить с живыми данными (.scratch/facts.md — после
# повторного спайка с рабочими токенами).
TYPE_NAMES: dict[int, str] = {
    1: "SEO/поиск",
    4: "медиа",
    5: "рекомендации",
    6: "каталог",
    7: "единая ставка",
    8: "аукцион/ручная",
    9: "медиа-снегопад",
    12: "авто-РК",
    15: "CRM/внешние",
}

# Campaign status codes -> display names (WB advert-api; сверить с фактами).
STATUS_NAMES: dict[int, str] = {
    9: "активна",
    11: "пауза",
    7: "завершена",
    8: "отменена",
    4: "готова",
    -1: "удалена",
}


def format_money(kopecks: int, currency: str) -> str:
    """Format integer kopecks as "12.34 <CURRENCY>" (dot separator, no grouping).

    Money in the DB is always non-negative integer kopecks of the cabinet's
    base currency; anything else (negative, non-int) is a bug -> ValueError.
    """
    if isinstance(kopecks, bool) or not isinstance(kopecks, int):
        raise TypeError(f"kopecks must be int, got {type(kopecks).__name__}")
    if currency not in CURRENCIES:
        raise ValueError(f"unsupported currency: {currency!r} (expected BYN or RUB)")
    if kopecks < 0:
        raise ValueError(f"kopecks must be non-negative, got {kopecks}")
    return f"{kopecks // 100}.{kopecks % 100:02d} {currency}"


def drr(sum_kopecks: int | None, sum_price_kopecks: int | None) -> str | None:
    """Advertising share of revenue: sum / sum_price, one decimal, e.g. "5.0%".

    None when the ratio is undefined: no revenue (0/None) or no spend value.
    """
    if sum_kopecks is None or not sum_price_kopecks:
        return None
    return f"{sum_kopecks / sum_price_kopecks * 100:.1f}%"


def rel_time(dt: datetime | None, now: datetime | None = None) -> str:
    """Relative age for freshness display.

    "только что" (<60s), "N мин назад" (<60m), "N ч назад" (<24h),
    "N д назад" otherwise; None (never collected) -> "нет данных".
    Naive datetimes are treated as UTC (all DB timestamps are UTC).
    """
    if dt is None:
        return "нет данных"
    if now is None:
        now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = (now - dt).total_seconds()
    if seconds < 60:  # future timestamps (clock skew) also count as fresh
        return "только что"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин назад"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ч назад"
    return f"{int(seconds // 86400)} д назад"


def type_name(type_code: int) -> str:
    """Human name for a WB campaign type; unknown codes render as "тип N"."""
    return TYPE_NAMES.get(type_code, f"тип {type_code}")


def status_name(status_code: int) -> str:
    """Human name for a WB campaign status; unknown codes render as "статус N"."""
    return STATUS_NAMES.get(status_code, f"статус {status_code}")
