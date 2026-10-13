"""Unit tests for presentation helpers (app/web/format.py)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.web.format import (
    STATUS_NAMES,
    TYPE_NAMES,
    drr,
    format_money,
    rel_time,
    status_name,
    type_name,
)

# --- format_money -----------------------------------------------------------


class TestFormatMoney:
    def test_zero(self):
        assert format_money(0, "BYN") == "0.00 BYN"

    def test_rubles_and_kopecks(self):
        assert format_money(123456, "RUB") == "1234.56 RUB"

    def test_less_than_one_unit(self):
        assert format_money(5, "BYN") == "0.05 BYN"

    def test_exact_unit(self):
        assert format_money(100, "RUB") == "1.00 RUB"

    def test_no_thousands_grouping(self):
        assert format_money(123456789, "BYN") == "1234567.89 BYN"

    @pytest.mark.parametrize("kopecks", [-1, -100, -123456])
    def test_negative_raises(self, kopecks: int):
        with pytest.raises(ValueError):
            format_money(kopecks, "BYN")

    @pytest.mark.parametrize("currency", ["USD", "EUR", "byn", "rub", "", "BYN "])
    def test_unsupported_currency_raises(self, currency: str):
        with pytest.raises(ValueError):
            format_money(100, currency)

    def test_non_int_raises(self):
        with pytest.raises(TypeError):
            format_money("100", "BYN")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            format_money(True, "BYN")  # type: ignore[arg-type]


# --- drr --------------------------------------------------------------------


class TestDrr:
    def test_basic(self):
        assert drr(2500, 50000) == "5.0%"

    def test_rounds_to_one_decimal(self):
        assert drr(1, 3) == "33.3%"
        assert drr(100, 30000) == "0.3%"

    def test_zero_spend(self):
        assert drr(0, 50000) == "0.0%"

    @pytest.mark.parametrize("sum_kopecks", [None])
    def test_none_sum(self, sum_kopecks):
        assert drr(sum_kopecks, 50000) is None

    @pytest.mark.parametrize("sum_price", [None, 0])
    def test_none_or_zero_sum_price(self, sum_price):
        assert drr(2500, sum_price) is None


# --- rel_time ---------------------------------------------------------------

_NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


class TestRelTime:
    @pytest.mark.parametrize("seconds", [0, 30, 59])
    def test_just_now(self, seconds: int):
        assert rel_time(_NOW - timedelta(seconds=seconds), now=_NOW) == "только что"

    @pytest.mark.parametrize("seconds", [60, 119, 3540, 3599])
    def test_minutes(self, seconds: int):
        minutes = seconds // 60
        assert rel_time(_NOW - timedelta(seconds=seconds), now=_NOW) == f"{minutes} мин назад"

    @pytest.mark.parametrize("seconds", [3600, 3660, 86399])
    def test_hours(self, seconds: int):
        hours = seconds // 3600
        assert rel_time(_NOW - timedelta(seconds=seconds), now=_NOW) == f"{hours} ч назад"

    @pytest.mark.parametrize("seconds", [86400, 172800, 259200])
    def test_days(self, seconds: int):
        days = seconds // 86400
        assert rel_time(_NOW - timedelta(seconds=seconds), now=_NOW) == f"{days} д назад"

    def test_none_means_no_data(self):
        assert rel_time(None) == "нет данных"

    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 10, 13, 11, 59, 0)  # == _NOW - 60s
        assert rel_time(naive, now=_NOW) == "1 мин назад"

    def test_future_timestamps_count_as_just_now(self):
        assert rel_time(_NOW + timedelta(seconds=90), now=_NOW) == "только что"

    def test_default_now(self):
        just = datetime.now(UTC) - timedelta(seconds=10)
        assert rel_time(just) == "только что"


# --- WB dictionaries --------------------------------------------------------


def test_type_names_known_values():
    assert TYPE_NAMES[1] == "SEO/поиск"
    assert TYPE_NAMES[4] == "медиа"
    assert TYPE_NAMES[5] == "рекомендации"
    assert TYPE_NAMES[6] == "каталог"
    assert TYPE_NAMES[7] == "единая ставка"
    assert TYPE_NAMES[8] == "аукцион/ручная"
    assert TYPE_NAMES[9] == "медиа-снегопад"
    assert TYPE_NAMES[12] == "авто-РК"
    assert TYPE_NAMES[15] == "CRM/внешние"


def test_type_name_unknown_falls_back():
    assert type_name(1) == "SEO/поиск"
    assert type_name(99) == "тип 99"


def test_status_names_and_unknown_falls_back():
    assert STATUS_NAMES[9] == "активна"
    assert STATUS_NAMES[11] == "пауза"
    assert STATUS_NAMES[7] == "завершена"
    assert STATUS_NAMES[8] == "отменена"
    assert STATUS_NAMES[4] == "готова"
    assert STATUS_NAMES[-1] == "удалена"
    assert status_name(42) == "статус 42"
