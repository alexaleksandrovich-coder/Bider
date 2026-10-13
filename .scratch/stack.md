# Стек: Bider (решение)

Дата: 2026-10-11. Выбор владельца: Python/FastAPI — как answers.robotay.ru.

## Решение

Один uvicorn-процесс: FastAPI-веб + встроенные asyncio-циклы сборщика. Postgres — своя база `bider` на VM answers.robotay.ru. Деплой за Caddy тем же способом, что answers.

| Слой | Выбор | Почему |
|---|---|---|
| Рантайм | Python 3.12+, uv | сервер уже на Python (uvicorn у answers), один рантайм |
| Веб | FastAPI + Jinja2 + HTMX | серверный рендер, без сборки фронтенда |
| Фон | asyncio-задачи в процессе (свои IntervalLoop) | 4 кабинета — нагрузка мизерная; Celery/arq избыточны |
| БД | PostgreSQL 16 + SQLAlchemy 2 + Alembic | история ставок/статистики, upsert-нагрузка; миграции |
| HTTP-клиент | httpx (async) | async + простые моки (respx) |
| Графики | Chart.js (локальный vendor-файл) | без CDN-зависимости |
| Тесты | pytest + pytest-asyncio + respx + TestClient | сеть всегда мок; живой API — только спайк-скрипт |
| Качество | ruff + mypy (basic) | единый стиль |
| Секреты | .env (Fernet-ключ, токены в БД Fernet-ом) | токены не в репо |

## Отвергнутое

- TS-монорепо как accountant-sellers-by (Next.js+BullMQ+Prisma): знаком владельцу, но на сервере появился бы второй рантайм и другой деплой-паттерн; веб-фронт здесь минимальный.
- Отдельный воркер-процесс: оправдан при 20+ кабинетах; при 4 — лишний systemd-юнит.
- Ленивый сбор по запросу: рвёт историю ставок, лимиты API жгутся на каждое открытие страницы.

## Версии на момент решения

Фиксируются в `uv.lock` при Task 2 (актуальные стабильные на дату установки): FastAPI 0.11x, SQLAlchemy 2.0.x, Alembic 1.1x, httpx 0.27+, pydantic-settings 2.x, cryptography (Fernet).
