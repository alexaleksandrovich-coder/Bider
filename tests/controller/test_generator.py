"""generate_hours table tests: greedy coverage selection + min_hours top-up.

The input shape mirrors queries.hours_heatmap: 24 rows
[{hour, orders, revenue_kopecks}] for hours 0..23.
"""

import pytest

from app.controller.generator import generate_hours


def heatmap(orders_by_hour: dict[int, int]) -> list[dict]:
    """A 24-row heatmap with the given order counts (0 elsewhere)."""
    return [
        {"hour": hour, "orders": orders_by_hour.get(hour, 0), "revenue_kopecks": 0}
        for hour in range(24)
    ]


@pytest.mark.parametrize(
    ("orders", "coverage_pct", "min_hours", "expected"),
    [
        # обычное распределение: 40+30+20 покрывают ровно 90% из 100 заказов
        ({10: 40, 11: 30, 12: 20, 13: 10}, 90, 3, [10, 11, 12]),
        # все заказы в 2 часах -> только они
        ({21: 70, 22: 30}, 90, 2, [21, 22]),
        # продаж нет -> дефолтные дневные часы 8..23
        ({}, 90, 6, list(range(8, 24))),
        # coverage 100 -> все часы с orders>0 (и ни одного нулевого)
        ({9: 5, 10: 3, 21: 2}, 100, 3, [9, 10, 21]),
        # min_hours добор: единственный час с заказами добивается до 6
        # крупнейшими (нулевые часы — по возрастанию номера часа)
        ({20: 100}, 90, 6, [0, 1, 2, 3, 4, 20]),
        # 4 часа покрывают 100% заказов, но min_hours=6 добивает нулевыми 1 и 2
        ({0: 10, 6: 10, 12: 10, 18: 10}, 90, 6, [0, 1, 2, 6, 12, 18]),
    ],
)
def test_generate_hours_table(orders, coverage_pct, min_hours, expected):
    assert generate_hours(heatmap(orders), coverage_pct, min_hours) == expected


def test_generate_hours_default_arguments():
    """Defaults (coverage 90, min 6): hours 10-12 cover 90%, top-up takes the
    next-largest ranked hours first (13 with 10 orders, then zero hours 0, 1)."""
    result = generate_hours(heatmap({10: 40, 11: 30, 12: 20, 13: 10}))
    assert result == [0, 1, 10, 11, 12, 13]


def test_generate_hours_takes_largest_first_under_coverage():
    """Жадный отбор: крупнейший час всегда входит, хвост добирает процент."""
    result = generate_hours(heatmap({8: 60, 9: 20, 10: 10, 11: 10}), coverage_pct=80, min_hours=1)
    assert result == [8, 9]  # 60+20=80 из 100 — 80% достигнуты
