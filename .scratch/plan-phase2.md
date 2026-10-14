# Фаза 2 (шаг 1): заказы → часы → расписания → лимиты — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development — субагент на задачу; после каждой: отчёт → верификация ZCode → «добро» владельца. Спек: `.scratch/spec-phase2.md` (читать вместе).

**Goal:** контроллер показов: заказы по часам → тепловая карта → подтверждаемые расписания (пауза/старт) + дневные лимиты, dry_run → live.

**Architecture:** те же два цикла в существующем процессе (orders 60с, controller 60с); новые таблицы orders/schedules/controller_log/app_settings; клиент statistics-api отдельно от advert-клиента.

**Tech Stack:** без изменений + statistics-api.wildberries.ru.

## Global Constraints

- Контроллер по умолчанию `dry_run`: любые pause/start/limits — только журнал (status="dry"), НИ ОДНОГО write-вызова к WB.
- Kill-switch (app_settings) проверяется перед каждым действием.
- Дедуп: ≤1 переключения на кампанию в час (controller_log).
- Часы — МСК (Europe/Moscow); деньги — копейки (orders price ×100: statistics-api отдаёт в единицах валюты — сверить спайком-2).
- Write-методы WB применяются в live ТОЛЬКО после явного добро владельца (переключение controller_mode).

---

### Task 1: Спайк-2 — формы записи и orders (без изменений боевых кампаний)

**Files:** `scripts/spike2_probe.py`, `.scratch/facts-phase2.md`.
- Песочница `advert-api-sandbox.wildberries.ru`: `GET /adv/v0/pause?id=<тестовая>`, `GET /adv/v0/start?id=<тестовая>` (кампании песочницы), GET/PUT `/api/advert/v0/daily-limits` — снять точные тела/параметры и заголовки лимитов. Боевой сервер: только чтение — `GET /api/advert/v0/daily-limits` (существует? тело?), `GET /api/v1/supplier/orders?dateFrom=<-2д>&flag=0` (лимит-заголовки, формат строки заказа, масштаб поля price, таймзона, srid).
- Step 3: прогон в песочнице (тестовый токен песочницы) + боевой read-only; Step 4: facts-phase2.md; Step 5: commit.
**Отчёт:** точные формы запросов/ответов, лимиты, масштаб money в orders.

### Task 2: Схема фазы 2

**Files:** `app/models.py` (+4 таблицы), `migrations/`, `tests/test_models_phase2.py`.
- `orders(srid text pk, cabinet_id fk, nm_id bigint, date date, hour int, price_kopecks bigint, date_time timestamptz, is_cancelled bool)` + индекс (cabinet_id, date, hour); `schedules(cabinet_id, advert_id, pk(cabinet_id, advert_id), hours jsonb, daily_limit_kopecks bigint null, enabled bool default false, updated_at, applied_at null)`; `controller_log(id, cabinet_id, advert_id null, action text, status text (ok/error/dry/skipped), detail text, created_at)`; `app_settings(key text pk, value jsonb)`.
- TDD: roundtrip'ы, JSONB hours, unique PK-нарушения. Commit `feat: phase2 schema`.

### Task 3: Сбор заказов

**Files:** `app/wb/statistics_client.py` (хост statistics-api, лимит orders 1/мин), `app/collector/orders.py`, tests.
- Метод `orders(date_from: datetime) -> list[dict]`; цикл 60с: инкремент flag=0 от последнего lastChangeDate (хранится в collection_log как у fullstats) с окном догона до 90 дней; дедуп по srid (upsert), is_cancelled по полям cancelButtonReason/storno (уточнить фактами); price → копейки (масштаб из facts-phase2).
- `queries.hours_heatmap(session, cabinet_id, days) -> [{hour, orders, revenue}]` (исключая отменённые).
- TDD: фейковый клиент (инкремент/дедуп/отменённые), тест queries. Commit `feat: orders collector`.

### Task 4: Страница «Часы»

**Files:** `app/web/pages.py`, `app/templates/hours.html`, tests.
- `/hours?cabinet=` — 24 столбца заказов (Chart.js bar) за 7/30 дней; переключатель периода; таблица-раскладка по часам с суммами.
- TDD: queries + smoke. Commit `feat: hours heatmap`.

### Task 5: Генератор часов + контроллер

**Files:** `app/controller/generator.py` (чистые), `app/controller/runner.py`, `app/wb/client.py` (+pause/start/daily_limits по facts-phase2), tests.
- `generate_hours(heatmap, coverage_pct=90, min_hours=6) -> list[int]` — жадный отбор топ-часов до покрытия %; tests таблично.
- Цикл `controller_forever` (60с/кабинет): kill-switch → skip; для каждой enabled schedule: желаемое состояние (час в hours? расход ≤ лимита?); фактический статус (adverts [id]); расходится и дедуп прошёл → действие (dry_run → log "dry" без вызова; live → pause/start/daily_limits_put + log); 3 подряд ошибки кабинета → пауза 15 мин.
- CPM: перерасход (stats за сегодня ≥ daily_limit) → пауза до 00:00 МСК.
- TDD: дедуп, kill-switch, dry_run, CPM-стоп, полуночный старт, 3 ошибки. Commit `feat: controller`.

### Task 6: Страница «Расписания»

**Files:** `app/web/pages.py`, `app/templates/schedules.html`, tests.
- Баннер режима (dry_run/live) + kill-switch/возобновить; таблица РК: чекбоксы часов (пре-заполнены generate_hours по кнопке «из данных»), поле дневного лимита, «Применить» (upsert schedules + applied_at); журнал контроллера (последние 100).
- TDD: smoke + применение + kill-switch endpoints. Commit `feat: schedules ui`.

### Task 7: Деплой + неделя dry_run

- Deploy (3-f цепочка), добавить orders-цикл в start_all/supervisor; 24ч наблюдения controller_log (только dry-записи); переключение в live — ОТДЕЛЬНОЕ добро владельца.

## Coverage

Стори 1–3→T3/T4, 3–5→T6, 6–7→T5/T6 (баннер/kill-switch), 8→T5/T6 (журнал), 9–10→T5, 11→T5 (дедуп), 12→T6 (баннер), 13→T3 (догон 90 дней), 14→T5.
