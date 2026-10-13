"""Tests for the WB rate limiter: token bucket, registry, path->class mapper."""

import pytest

from app.wb.constants import LIMITS, klass
from app.wb.limiter import AsyncTokenBucket, LimiterRegistry


class TestAsyncTokenBucket:
    async def test_fourth_call_within_minute_sleeps(self, fake_clock, spy_sleep):
        b = AsyncTokenBucket(3, 60, clock=fake_clock, sleep=spy_sleep)  # 3/min, burst 3
        for _ in range(4):
            await b.acquire()
        # 3 calls consume the initial burst; the 4th must wait ~a full period/3.
        assert spy_sleep.total >= 20.0

    async def test_initial_burst_does_not_sleep(self, fake_clock, spy_sleep):
        b = AsyncTokenBucket(3, 60, clock=fake_clock, sleep=spy_sleep)
        for _ in range(3):
            await b.acquire()
        assert spy_sleep.calls == []

    async def test_refilled_bucket_acquires_without_sleep(self, fake_clock, spy_sleep):
        b = AsyncTokenBucket(3, 60, clock=fake_clock, sleep=spy_sleep)
        for _ in range(3):
            await b.acquire()
        fake_clock.advance(61)  # more than a full period -> bucket refilled
        await b.acquire()  # must not sleep
        assert spy_sleep.last is None or spy_sleep.last == 0

    async def test_pacing_total_matches_rate(self, fake_clock, spy_sleep):
        # 1/sec, burst 1: five acquires -> 4 waits of ~1s.
        b = AsyncTokenBucket(1, 1, clock=fake_clock, sleep=spy_sleep)
        for _ in range(5):
            await b.acquire()
        assert 3.9 <= spy_sleep.total <= 4.1


class TestLimiterRegistry:
    def test_same_cabinet_and_class_returns_same_bucket(self, fake_clock, spy_sleep):
        reg = LimiterRegistry(clock=fake_clock, sleep=spy_sleep)
        assert reg.bucket(1, "fullstats") is reg.bucket(1, "fullstats")

    def test_different_cabinets_get_different_buckets(self, fake_clock, spy_sleep):
        reg = LimiterRegistry(clock=fake_clock, sleep=spy_sleep)
        assert reg.bucket(1, "fullstats") is not reg.bucket(2, "fullstats")

    def test_different_classes_get_different_buckets(self, fake_clock, spy_sleep):
        reg = LimiterRegistry(clock=fake_clock, sleep=spy_sleep)
        assert reg.bucket(1, "fullstats") is not reg.bucket(1, "budget")

    def test_bucket_uses_configured_limit(self, fake_clock, spy_sleep):
        reg = LimiterRegistry({"k": (2, 10)}, clock=fake_clock, sleep=spy_sleep)
        b = reg.bucket(7, "k")
        assert (b.requests, b.period_sec) == (2, 10)

    def test_unknown_class_raises_value_error(self, fake_clock, spy_sleep):
        reg = LimiterRegistry(clock=fake_clock, sleep=spy_sleep)
        with pytest.raises(ValueError):
            reg.bucket(1, "nope")

    def test_default_limits_cover_all_classes(self, fake_clock, spy_sleep):
        reg = LimiterRegistry(clock=fake_clock, sleep=spy_sleep)
        for name in LIMITS:
            assert reg.bucket(1, name) is reg.bucket(1, name)


class TestKlassMapper:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/adv/v3/fullstats", "fullstats"),
            ("/api/advert/v2/budget", "budget"),
            ("/api/advert/v1/bids/min", "bids_min"),
            ("/api/advert/v0/bids/recommendations", "recommendations"),
            ("/api/advert/v2/adverts", "adverts"),
            ("/adv/v1/promotion/count", "count"),
            ("/adv/v1/balance", "balance"),
            ("/adv/v0/normquery/list", "normquery"),
            ("/api/advert/v1/normquery/stats", "normquery"),
        ],
    )
    def test_known_paths(self, path, expected):
        assert klass(path) == expected

    def test_unknown_path_raises_value_error(self):
        with pytest.raises(ValueError):
            klass("/api/adv/definitely/unknown")
