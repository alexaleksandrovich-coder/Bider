"""Read-only WB data collector.

Этот пакет собирает данные из WB advert-api ТОЛЬКО через методы чтения
(app.wb.client.WbClient): окна догона (windows), фоновые циклы (loops) и
задачи сбора (runner). НИКАКИХ write-методов WB (ставки, пауза/старт,
пополнение, минус-фразы) — фаза 1 строго read-only, см. .scratch/spec.md.
"""

from app.collector.windows import next_window

__all__ = ["next_window"]
