"""Fullstats window planning: how far back to (re)fetch statistics.

Pure functions only — no IO, no clocks; `now` is always passed in by the
caller. All datetimes must be timezone-aware and are compared by ``.date()``
only (stat dates are Moscow-calendar days, see Global Constraints).

Window semantics are INCLUSIVE of both ends (WB fullstats takes
beginDate..endDate inclusive): the default window is the last 7 calendar
days (today-6 .. today) and ``max_days`` caps the window at that many
calendar days inclusive. These match the authoritative test table in
tests/collector/test_windows.py.
"""

from datetime import date, datetime, timedelta

DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAX_DAYS = 31


def next_window(
    last_success: datetime | None, now: datetime, *, max_days: int = DEFAULT_MAX_DAYS
) -> tuple[date, date]:
    """Return the (begin, end) fullstats window for one campaign.

    - ``last_success is None`` (never collected): the default window of
      ``DEFAULT_WINDOW_DAYS`` calendar days ending today.
    - otherwise: resume from the last successful day; a timestamp in the
      future relative to ``now`` (clock skew) collapses to just today so a
      skewed clock never triggers a backfill.
    - the window is capped at ``max_days`` calendar days inclusive (WB
      fullstats accepts at most ~31 days per request).
    """
    end = now.date()
    if last_success is None:
        begin = end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    else:
        begin = min(last_success.date(), end)  # future timestamp: never backfill
    if (end - begin).days >= max_days:  # more than max_days inclusive days
        begin = end - timedelta(days=max_days - 1)
    return begin, end
