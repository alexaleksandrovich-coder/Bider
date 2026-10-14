"""Table tests for the pure fullstats window planner (no IO, no clocks).

All datetimes are timezone-aware; the planner compares by .date() only.
Window semantics are INCLUSIVE of both ends: a "7-day window" spans
(today-6 .. today) and the max_days cap keeps at most `max_days` calendar
days inclusive — these expectations are the authoritative spec (the test
table from Task 6).
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.collector.windows import next_window

_MSK = timezone(timedelta(hours=3))
_NOW = datetime(2026, 10, 13, 10, 0, tzinfo=_MSK)


@pytest.mark.parametrize(
    ("last_success", "expected"),
    [
        # No prior success: first collection takes the full 31-day depth
        # (retrospective history for the dashboard's "30 days" period).
        (None, (date(2026, 9, 13), date(2026, 10, 13))),
        # Recent success: resume from that day (overlap of one day is fine,
        # stats upserts are idempotent).
        (datetime(2026, 10, 10, 18, 30, tzinfo=_MSK), (date(2026, 10, 10), date(2026, 10, 13))),
        # 42 days back: capped at 31 days inclusive -> begin = end - 30.
        (datetime(2026, 9, 1, 0, 0, tzinfo=_MSK), (date(2026, 9, 13), date(2026, 10, 13))),
        # Exactly 31 days back would be 32 inclusive days -> also capped.
        (datetime(2026, 9, 12, 23, 0, tzinfo=_MSK), (date(2026, 9, 13), date(2026, 10, 13))),
        # Exactly 30 days back = 31 inclusive days -> no cap needed.
        (datetime(2026, 9, 13, 23, 0, tzinfo=_MSK), (date(2026, 9, 13), date(2026, 10, 13))),
        # Future timestamp (clock skew): do not backfill, just today.
        (datetime(2026, 10, 20, 9, 0, tzinfo=_MSK), (date(2026, 10, 13), date(2026, 10, 13))),
    ],
)
def test_next_window_table(
    last_success: datetime | None, expected: tuple[date, date]
) -> None:
    assert next_window(last_success, _NOW) == expected


def test_next_window_custom_cap() -> None:
    """max_days is injectable: default 7-day window shrinks to the cap."""
    assert next_window(None, _NOW, max_days=3) == (date(2026, 10, 11), date(2026, 10, 13))


def test_next_window_accepts_other_timezones() -> None:
    """Only .date() comparisons matter: a UTC timestamp for the same instant
    (07:00 UTC == 10:00 MSK) gives the same window."""
    now_utc = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
    last = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
    assert next_window(last, now_utc) == (date(2026, 10, 10), date(2026, 10, 13))
