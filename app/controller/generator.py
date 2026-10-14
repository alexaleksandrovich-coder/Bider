"""Чистый генератор активных часов из тепловой карты заказов (phase 2).

Вход — результат queries.hours_heatmap: ровно 24 строки
[{hour, orders, revenue_kopecks}] за выбранный период (часы 0..23 по МСК).

Алгоритм (spec-phase2, Implementation Decisions → Расписания): жадный отбор
часов по убыванию orders, пока накопленная доля заказов не достигнет
coverage_pct% всех заказов; если выбранных часов получилось меньше min_hours —
добор крупнейшими до min_hours. Продаж нет совсем — дневные часы 8..23 по
умолчанию. Функция чистая: без БД, без времени, детерминированная (равные
orders упорядочиваются по возрастанию номера часа).
"""

_DEFAULT_DAY_HOURS = range(8, 24)  # продаж нет — дневные часы по умолчанию


def generate_hours(
    heatmap: list[dict],
    coverage_pct: int = 90,
    min_hours: int = 6,
) -> list[int]:
    """Часы, покрывающие coverage_pct% заказов, но не меньше min_hours.

    Возвращает отсортированный список часов 0..23 (пустой вход/нулевые продажи
    -> дефолт 8..23). Целочисленное сравнение covered*100 >= total*coverage_pct
    — без дробных ошибок округления.
    """
    ranked = sorted(
        heatmap,
        key=lambda row: (-_orders(row), _hour(row)),
    )
    total = sum(_orders(row) for row in heatmap)
    if total <= 0:
        # продаж нет — дневные часы по умолчанию
        return list(_DEFAULT_DAY_HOURS)

    selected: list[int] = []
    covered = 0
    for row in ranked:
        if covered * 100 >= total * coverage_pct:
            break
        selected.append(_hour(row))
        covered += _orders(row)

    for row in ranked:  # добор до min_hours крупнейшими из оставшихся
        if len(selected) >= min_hours:
            break
        hour = _hour(row)
        if hour not in selected:
            selected.append(hour)
    return sorted(selected)


def _hour(row: dict) -> int:
    return int(row.get("hour", 0))


def _orders(row: dict) -> int:
    return int(row.get("orders", 0))
