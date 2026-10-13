# Bider MVP-1 (спайк + сборщик + дашборд) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development — свежий субагент на каждую задачу; после каждой задачи: отчёт субагента → верификация ZCode → «добро» владельца. Шаги с чекбоксами `- [ ]`.

**Goal:** Read-only фундамент автобиддера: спайк живого advert-api, сбор данных 4 кабинетов WB (3 BY + 1 RU) в PostgreSQL и веб-дашборд за логином.

**Architecture:** один uvicorn-процесс: FastAPI (Jinja2+HTMX) + встроенные asyncio-циклы сбора с токен-бакет рейт-лимитером на (кабинет × класс методов); PostgreSQL 16, SQLAlchemy 2 + Alembic; токены в БД под Fernet.

**Tech Stack:** Python 3.12+, uv, FastAPI, SQLAlchemy 2, Alembic, httpx, respx, pytest, HTMX, Chart.js (vendor).

**Spec:** `.scratch/spec.md` (читать вместе с планом; план спорит от спека).

## Global Constraints

- Только официальный `advert-api.wildberries.ru`; read-only: никаких write-методов (bids/pause/start/deposit/set-minus) — фаза 1.
- Деньги — integer в копейках базовой валюты кабинета (`*_kopecks`); отображение — валюта кабинета, 2 знака.
- Даты статистики — по Москве (UTC+3); `captured_at` — UTC.
- Секреты только в `.env` (Fernet-ключ, админ, DATABASE_URL); токены кабинетов — в БД зашифрованные. Ничего секретного в репо.
- Лимиты WB: fullstats 3/мин, budgets 20/мин, bids_min 20/мин, recommendations 5/мин, adverts 5/с, count 5/с, balance 1/с — из `.scratch/facts.md` после спайка (в `app/wb/constants.py`, правится только оттуда).
- Каждый цикл идемпотентен; 3 подряд сбоя цикла → пауза 15 мин + строка в collection_log.
- Коммиты после каждого шага «тесты зелёные», формат сообщений: `feat:|test:|chore: …`.

---

### Task 1: Спайк живого API → facts.md

**Files:**
- Create: `scripts/spike_probe.py`, `.scratch/facts.md`
- Test: ручной прогон по 1 BY + 1 RU токену (сеть; автоматических тестов нет — скрипт одноразовый, помечен throwaway)

**Interfaces:**
- Produces: `.scratch/facts.md` с секциями `## BY <имя>` / `## RU <имя>`: статус каждого метода, `/api/advert/v1/config` (currency, currencyCode, cpcStep, cpmStep, minDailyLimit, minTopUp), реальные типы/статусы РК, sample-поля fullstats (есть ли boosterStats.avg_position), заголовки X-Ratelimit. Этот файл — единственный источник констант для `app/wb/constants.py` (Task 4).

- [ ] **Step 1: написать скрипт** (read-only, только GET/чтение; между вызовами sleep 1s; токены из env: `WB_TOKEN_BY1`, `WB_TOKEN_RU1`)

```python
"""Throwaway spike: read-only probe of WB advert-api. Usage:
WB_TOKEN_BY1=... WB_TOKEN_RU1=... uv run python scripts/spike_probe.py > .scratch/facts_raw.md
"""
import asyncio, json, os, time
import httpx

HOST = "https://advert-api.wildberries.ru"
PROBES = [
    ("GET", "/adv/v1/promotion/count", None),
    ("GET", "/api/advert/v1/config", None),
    ("GET", "/adv/v1/balance", None),
    ("GET", "/api/advert/v2/budget", None),  # POST-чтение: body добирается из count, см. шаг 2
]

async def probe(client: httpx.AsyncClient, token: str, name: str, out: list[str]):
    out.append(f"## {name}\n")
    h = {"Authorization": token}
    for method, path, body in PROBES:
        t0 = time.monotonic()
        try:
            r = await client.request(method, HOST + path, headers=h, json=body, timeout=30)
            dt = int((time.monotonic() - t0) * 1000)
            out.append(f"### {method} {path} -> {r.status_code} ({dt}ms)\n")
            out.append(f"ratelimit: remaining={r.headers.get('X-Ratelimit-Remaining')} limit={r.headers.get('X-Ratelimit-Limit')} retry={r.headers.get('X-Ratelimit-Retry')}\n")
            out.append("```json\n" + json.dumps(safe_json(r), ensure_ascii=False, indent=1)[:4000] + "\n```\n")
        except Exception as e:
            out.append(f"### {method} {path} -> EXC {e!r}\n")
        await asyncio.sleep(1)
    # follow-ups (adverts/budget/fullstats/bids_min/recommendations) — добавляются
    # в Step 2 ниже: id берутся из ответа count, форматы ответов фиксируем как факт.

def safe_json(r: httpx.Response):
    try: return r.json()
    except Exception: return {"text": r.text[:1000]}

async def main():
    for env, name in [("WB_TOKEN_BY1", "BY"), ("WB_TOKEN_RU1", "RU")]:
        tok = os.environ.get(env)
        if not tok: print(f"skip {env}"); continue
        async with httpx.AsyncClient() as c:
            out: list[str] = []
            await probe(c, tok, name, out)
            print("\n".join(out))
asyncio.run(main())
```

- [ ] **Step 2: дополнить follow-ups**: из ответа count взять id активных РК (status 9/11, типы из ответа) → `GET /api/advert/v2/adverts?ids=...` (≤50), `POST /api/advert/v2/budget` c `{"ids": [...]}`, `GET /adv/v3/fullstats?ids=...&beginDate=<вчера>&endDate=<сегодня>`, `POST /api/advert/v1/bids/min` c nm_id из adverts, `GET /api/advert/v0/bids/recommendations` (params: type/payment_type из РК; если 4xx — зафиксировать тело ошибки как факт).
- [ ] **Step 3: прогнать** для BY и RU токенов (владелец даёт токены в env, НЕ в чат/репо).
- [ ] **Step 4: заполнить `.scratch/facts.md`** по шаблону: секция на кабинет + вывод «подтверждено/нет» по каждому пункту Interfaces; именно из него Task 4 берёт лимиты/шаги/масштаб.
- [ ] **Step 5: коммит** `chore: spike probe + facts` (токены не коммитить).

**Отчёт субагента:** факты по 8 пунктам (BY-токен работает? валюта? шаги? minDailyLimit? типы РК? avg_position? реальные пути? лимитные заголовки?), ссылка на facts_raw.md.

### Task 2: Скелет проекта + health

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/config.py`, `app/main.py`, `tests/__init__.py`, `tests/test_health.py`, `.env.example`, `docker-compose.dev.yml`, `.gitignore`, `README.md`
- Команда: `git init` (репо пустое, только logs/ и .scratch/)

**Interfaces:**
- Produces: `app.config.Settings` (pydantic-settings: `database_url: str`, `fernet_key: str`, `admin_username: str`, `admin_password: str`, `session_days: int = 30`, `raw_log_days: int = 14`), `app.main.create_app() -> FastAPI`; `GET /health` → `{"ok": true, "version": "0.1.0"}`.

- [ ] **Step 1: тест** `tests/test_health.py`:

```python
from app.main import create_app
from fastapi.testclient import TestClient

def test_health_ok():
    c = TestClient(create_app())
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.1.0"}
```

- [ ] **Step 2: прогнать** `uv run pytest tests/test_health.py -v` → FAIL (ModuleNotFoundError).
- [ ] **Step 3: реализация**: `uv init --app`; deps — точный список: `fastapi uvicorn jinja2 python-multipart sqlalchemy alembic asyncpg httpx pytest pytest-asyncio respx cryptography pydantic-settings`; `app/config.py` = `Settings(BaseSettings)` c `model_config = SettingsConfigDict(env_file=".env", env_prefix="BIDER_")` и полями `database_url: str`, `fernet_key: str`, `admin_username: str`, `admin_password: str`, `session_days: int = 30`, `raw_log_days: int = 14`, `run_collector: bool = False`; `create_app()` регистрирует `/health`.
- [ ] **Step 4: тесты зелёные** `uv run pytest -v` → PASS.
- [ ] **Step 5: инфра-файлы**: `.env.example` (все BIDER_* без секретов), `docker-compose.dev.yml` (postgres:16-alpine, порт хоста **5436**, healthcheck, volume), `.gitignore` (`.env`, `.scratch/facts_raw*`, `__pycache__`, `.venv`), `git init` + первый коммит `chore: scaffold`.
- [ ] **Step 6: ruff** `uv run ruff check app tests` → 0 ошибок.

### Task 3: Схема БД + репозитории

**Files:**
- Create: `app/db.py`, `app/models.py`, `app/crypto.py`, `migrations/` (alembic), `tests/conftest.py`, `tests/test_models.py`
- Alembic: `uv run alembic init migrations`; env указывает на `app.models.Base.metadata`

**Interfaces:**
- Produces: модели с ТОЧНЫМИ именами из spec «Модель данных» (`User, Session, Cabinet, Campaign, BidHistory, Stat, Position, Budget, Balance, AuctionSnapshot, Cluster, CollectionLog, RawResponse`); `app.crypto.encrypt_token(s)->str`, `decrypt_token(s)->str`; `app/db.SessionLocal`; fixture `db_session` в conftest (транзакция + rollback, Postgres из docker-compose.dev на localhost:5436).
- Consumes: `Settings.database_url` (Task 2), `Settings.fernet_key` (Task 2).

- [ ] **Step 1: тест roundtrip**: создать Cabinet с `encrypt_token`, прочитать, `decrypt_token` == исходный; уникальность `users.username` нарушается второй вставкой.
- [ ] **Step 2: прогнать** → FAIL (нет моделей).
- [ ] **Step 3: модели** строго по спеку (все pk/уникальности/NOT NULL; у `Campaign` составной pk `(cabinet_id, advert_id)`; у `Stat` составной pk `(cabinet_id, advert_id, nm_id, date, app_type)`; у `BidHistory` bigserial append-only; `RawResponse.payload = JSONB`).
- [ ] **Step 4: alembic revision --autogenerate + upgrade head** на dev-Postgres.
- [ ] **Step 5: тесты зелёные** (roundtrip + PK-нарушения + JSONB roundtrip) → PASS; коммит `feat: schema+crypto`.

### Task 4: WB-клиент + рейт-лимитер

**Files:**
- Create: `app/wb/__init__.py`, `app/wb/constants.py`, `app/wb/limiter.py`, `app/wb/client.py`, `app/wb/errors.py`
- Test: `tests/wb/test_limiter.py`, `tests/wb/test_client.py`

**Interfaces:**
- Produces:
  - `AsyncTokenBucket(rate_per_min: int, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable] = asyncio.sleep)` + `async def acquire(self) -> None`
  - `LimiterRegistry.bucket(cabinet_id: int, klass: str) -> AsyncTokenBucket` (по одному на пару)
  - `WbApiError(status: int, detail: str, retry_after: float | None)`
  - `WbClient(token: str, limiter: LimiterRegistry, http: httpx.AsyncClient | None = None)`; методы: `ping() -> dict`, `promotion_count() -> dict`, `adverts(ids: list[int]) -> list[dict]`, `budgets(advert_ids: list[int]) -> list[dict]`, `balance() -> dict`, `fullstats(advert_ids: list[int], begin: date, end: date) -> list[dict]`, `bids_min(nm_ids: list[int]) -> dict`, `recommendations(params: dict) -> dict`, `normquery_list(advert_id: int) -> dict`, `normquery_stats(body: dict) -> dict`
- Consumes: лимиты/шаги из `.scratch/facts.md` → `app/wb/constants.py`: `HOST`, `LIMITS = {"fullstats": (3, 60), "budget": (20, 60), "bids_min": (20, 60), "recommendations": (5, 60), "adverts": (5, 1), "count": (5, 1), "balance": (1, 1), "normquery": (5, 60)}` (числа могут быть правлены по facts.md), `klass(path)` — маппер путь→класс.

- [ ] **Step 1: тест лимитера** (фейковые часы, sleep-шпион):

```python
async def test_bucket_paces_calls(fake_clock, spy_sleep):
    b = AsyncTokenBucket(3, clock=fake_clock, sleep=spy_sleep)  # 3/мин
    starts = []
    for _ in range(4):
        await b.acquire(); starts.append(fake_clock())
    assert spy_sleep.total >= 20.0  # 4-й вызов ждёт ≥20с

async def test_bucket_refills_over_time(fake_clock, spy_sleep):
    b = AsyncTokenBucket(3, clock=fake_clock, sleep=spy_sleep)
    for _ in range(3): await b.acquire()
    fake_clock.advance(61)
    await b.acquire()          # без сна
    assert spy_sleep.last is None or spy_sleep.last == 0
```

- [ ] **Step 2: прогнать** → FAIL.
- [ ] **Step 3: реализация лимитера**: классический token bucket (burst = rate), пополнение `rate/period * dt`; при пустом — `sleep(needed)`.
- [ ] **Step 4: тест клиента** (respx): 200 → распарсенный json; **429** c `X-Ratelimit-Retry: 2` → ровно один sleep(2) и повтор, затем успех; 401/5xx → `WbApiError` c телом `detail`; заголовок Authorization без Bearer.
- [ ] **Step 5: реализация клиента**: `_request(klass, method, path, **kw)` = `await limiter.bucket(cab, klass).acquire()` → отправка → обработка 429 (один ретрай после retry_after, второй 429 — исключение) → `raise_for_status` → json. Все публичные методы — тонкие обёртки путей.
- [ ] **Step 6: тесты зелёные + ruff**; коммит `feat: wb client + limiter`.

### Task 5: Парсеры + запись данных

**Files:**
- Create: `app/wb/parsers.py`, `app/repos.py`
- Test: `tests/wb/test_parsers.py`, `tests/test_repos.py`
- Fixtures: `tests/fixtures/fullstats_sample.json`, `adverts_sample.json`, `budget_sample.json` — из `.scratch/facts_raw.md` (спайк)

**Interfaces:**
- Produces:
  - `parse_adverts(payload: list[dict]) -> list[CampaignRow]`; `CampaignRow = dict(advert_id:int, name:str, type:int, status:int, payment_type:str, bid_type:str, search:bool, recommendations:bool, change_time:datetime|None, bids: list[BidRow])`; `BidRow = dict(nm_id:int, placement:str, bid_kopecks:int)` (из `bids_kopecks.search/recommendations`, placement = 'search'|'recommendations')
  - `parse_fullstats(payload: list[dict]) -> tuple[list[StatRow], list[PositionRow]]`; `StatRow = dict(advert_id, nm_id, date: date, app_type: str, views, clicks, atbs, orders, shks, sum_kopecks, sum_price_kopecks, cpc_kopecks, canceled)` — суммирование по appType-веткам `days[]`; `PositionRow = dict(advert_id, nm_id, date, avg_position: float)` из `boosterStats[]`
  - `parse_budget(payload) -> list[BudgetRow]` (`advert_id, cash, netting, total` → *_kopecks)
  - `repos.upsert_campaigns(session, cabinet_id, rows)`, `repos.append_bids(session, cabinet_id, rows, captured_at)`, `repos.upsert_stats(session, cabinet_id, stat_rows)`, `repos.upsert_positions(...)`, `repos.insert_budgets(...)`, `repos.insert_balance(...)`, `repos.insert_snapshot(...)`, `repos.log_collection(session, ...)`
- Consumes: модели Task 3, client/parsers Task 4.

- [ ] **Step 1: тест парсера fullstats** на фикстуре: ровно N строк, поля совпадают с ручным расчётом по дням/appType; double-write идемпотентен (`upsert_stats` дважды → то же число строк).
- [ ] **Step 2: прогнать** → FAIL → **Step 3: реализация парсеров** (чистые функции, без IO).
- [ ] **Step 4: тест репо**: upsert_campaigns дважды (изменили name/status) → 1 строка с новыми значениями; append_bids дважды → 2 строки; коммит `feat: parsers+repos`.

### Task 6: Сборщик — циклы и догон окон

**Files:**
- Create: `app/collector/__init__.py`, `app/collector/loops.py`, `app/collector/runner.py`
- Modify: `app/main.py` (lifespan: `runner.start(settings.run_collector)`)
- Test: `tests/collector/test_windows.py`, `tests/collector/test_loops.py`

**Interfaces:**
- Produces:
  - `next_window(last_success: datetime | None, now: datetime, *, max_days: int = 31) -> tuple[date, date]` — чистая функция: None → (вчера−6, сегодня); иначе (last_success.date(), сегодня), cap 31 день.
  - `IntervalLoop(name: str, cabinet_id: int, period_sec: int, fn: Callable[[], Awaitable[None]], *, clock, sleep)`; `async run_forever()`, `async run_once()`; счётчик подряд ошибок: ≥3 → sleep 900s, строка в collection_log.
  - `runner.LOOPS_SPEC: list[LoopSpec]` — 7 циклов из spec (campaigns 300s, budgets 300s, balance 300s, fullstats 1200s + суточный ретро-проход, bids_min 86400s, auction_snapshot 3600s, clusters 86400s) + ночной чистильщик raw_responses (`settings.raw_log_days`).
- Consumes: client Task 4, parsers/repos Task 5, models Task 3.

- [ ] **Step 1: тест next_window** (табличный): None→окно 7 дней; last_success=3 дня назад → (тот день, сегодня); last_success=40 дней назад → cap 31; коммит `test: windows` → **Step 2** прогнать → FAIL → **Step 3** реализовать.
- [ ] **Step 4: тест цикла** с фейковым временем: fn вызывается периодически; при 3 исключениях подряд — длинный sleep и запись лога; `run_once` не зацикливается.
- [ ] **Step 5: реализация** каждого цикла: campaigns (count → ids → adverts пакетами ≤50 → upsert + append_bids), budgets, balance, fullstats (для активных РК: окно next_window от последнего успешного по этой РК; приоритет — последний запуск раньше), bids_min, auction_snapshot (сырой payload в raw_responses + jsonb в auction_snapshots), clusters (только РК с bid_type=manual).
- [ ] **Step 6: интеграционный тест**: фейковый client (заглушки с фикстурами) + реальный Postgres → после `run_once` всех циклов в БД есть кампании/ставки/статы; коммит `feat: collector`.

### Task 7: Auth + каркас страниц

**Files:**
- Create: `app/web/__init__.py`, `app/web/auth.py`, `app/templates/base.html`, `app/templates/login.html`, `app/static/vendor/htmx.min.js`, `app/static/css/app.css`
- Modify: `app/main.py` (Jinja2Templates, static mount, роуты login/logout)
- Test: `tests/web/test_auth.py`

**Interfaces:**
- Produces: `hash_password(pw: str) -> str` (scrypt, salt в строке), `verify_password(pw, stored) -> bool`, `create_session(user_id) -> token`, `require_user` (FastAPI dependency: нет валидной сессии → 303 `/login`); routes: `GET/POST /login`, `POST /logout`; seed-админ при старте если таблица users пуста (из Settings).
- Consumes: models Task 3, Settings Task 2.

- [ ] **Step 1: тесты**: неавторизованный `GET /` → 303 `/login`; POST login неверный пароль → форма с ошибкой; верный → 303 `/` + cookie; logout убивает сессию. → **Step 2** FAIL → **Step 3** реализация.
- [ ] **Step 4: base.html**: nav (Обзор, Кабинеты, Товары, Health), HTMX script, `app.css`; login.html — форма. Коммит `feat: auth`.

### Task 8: Дашборд — обзор, кабинеты, список РК

**Files:**
- Create: `app/web/pages.py`, `app/web/format.py`, `app/templates/{overview.html, cabinet.html, admin_cabinets.html}`, `app/queries.py`
- Test: `tests/web/test_pages.py`, `tests/test_queries.py`, `tests/test_format.py`

**Interfaces:**
- Produces:
  - `format_money(kopecks: int, currency: str) -> str` («1 234,56 BYN»), `drr(sum_kopecks, sum_price_kopecks) -> str | None`
  - `queries.overview(session) -> list[CabinetOverview]` (`cabinet, spent_today/7d/30d: int, balance: int|None, freshness: datetime|None`)
  - `queries.cabinet_campaigns(session, cabinet_id, status: str|None) -> list[...]`
  - routes: `GET /` (обзор), `GET /cabinets/{id}` (РК, фильтр статуса), `GET/POST /admin/cabinets` (добавить кабинет: name, wb_type, token → encrypt_token; отображение только имя/тип/валюта/свежесть, токен никогда не рендерится)
- Consumes: repos/models, auth Task 7.

- [ ] **Step 1: тесты format_money** (грани: 0, копейки <100, отрицательные не бывают — assert) и **drr** (None при sum_price=0). → реализация → PASS.
- [ ] **Step 2: тест queries** на сид-данных (Postgres): расходы по периодам считаются из `stats.sum_kopecks` по датам МСК; свежесть = max(collection_log finished_at успешных циклов).
- [ ] **Step 3: тест страниц**: сид + TestClient с залогиненной сессией → 200, в HTML есть суммы и имена кабинетов; `/admin/cabinets` POST добавляет кабинет, токен в БД ≠ открытый текст. Коммит `feat: dashboard overview+cabinets`.

### Task 9: Дашборд — РК-детально, товары, health

**Files:**
- Create: `app/templates/{campaign.html, items.html, health.html}`, `app/web/charts.py` (данные графиков в JSON для Chart.js)
- Modify: `app/queries.py` (campaign_detail, items_summary, health_status)
- Test: `tests/web/test_campaign_page.py`, `tests/test_queries_health.py`

**Interfaces:**
- Produces:
  - `GET /cabinets/{id}/campaigns/{advert_id}?days=7|30` — блоки: сводные метрики за период; таблица по товарам (views/clicks/orders/shks/sum/sum_price/DRR); график ставок (bid_history по placement); график позиций (positions.avg_position); график расхода по дням; для CPC — последний budget (cash/total); для manual — кластеры (norm_query, bid, stats); raw-кнопка «снапшоты аукциона» (последние 5 jsonb в details).
  - `GET /items?cabinet={id}` — агрегат по nm_id: расход/выручка/DRR по всем РК, сортировка по расходу.
  - `GET /health` — по каждому циклу: имя, кабинет, последний успех, период, ошибки подряд; таблица collection_log (последние 100).
- Consumes: всё предыдущее.

- [ ] **Step 1: тест queries.campaign_detail** (окна 7/30, агрегаты по nm, серии графиков по датам без дыр → null). → реализация.
- [ ] **Step 2: smoke-тесты страниц** 200 + ключевые строки. → реализация шаблонов (Chart.js: `<canvas>` + `new Chart(..., series_json)`).
- [ ] **Step 3: e2e smoke**: сид → залогиниться → обзор → кабинет → РК → health, ни одного 5xx; ruff; коммит `feat: detail+items+health`.

### Task 10: Деплой на VM answers.robotay.ru + боевой прогон

**Files:**
- Create: `deploy/{bider.service (или compose-файл — по ответу владельца), CADDY-snippet.md}`, `.env.prod.example`
- Test: живая проверка по чек-листу (ручная)

**Interfaces:** Consumes: готовое приложение Task 9; ответы владельца на Open Items 2–4 из spec.

- [ ] **Step 1: прод-конфиг**: `.env.prod` на сервере (DATABASE_URL postgres на VM, FERNET_KEY сгенерирован, админ-пароль сгенерирован), Postgres (существующий или docker), `alembic upgrade head`.
- [ ] **Step 2: юнит**: systemd (Restart=always) ИЛИ compose-сервис — повторить способ answers; Caddy: домен/путь из ответа владельца, reverse_proxy на 127.0.0.1:<порт>.
- [ ] **Step 3: добавить 4 кабинета** через `/admin/cabinets` (токены вставляет владелец; проверка: cabinet появился, свежесть через ≤10 мин).
- [ ] **Step 4: 24ч наблюдения**: collection_log без циклов ошибок ≥3; в facts зафиксировать расхождения боевых ответов; выключить raw_responses при необходимости (settings.raw_log_days=0 — по решению на ревью).
- [ ] **Step 5: ревью владельца** данных дашборда против кабинета WB (выборочная сверка 2–3 РК).

---

## Соответствие спеку (coverage)

- Стори 1→Task 8 (admin_cabinets), 2→Task 8 (overview), 3→Task 8 (cabinet), 4→Task 9 (campaign), 5→Task 9 (график ставок), 6→Task 9 (позиции), 7→Task 9 (CPC бюджет), 8→Task 9 (кластеры), 9→Task 9 (items), 10→Task 7, 11→Task 9 (health), 12→Task 6 (next_window), 13→Task 3 (crypto), 14→Task 8 (format), 15→Task 1, 16→Task 4/6 (limiter, пауза), 17→Task 6 (raw_responses), 18→Tasks 4–6 (тесты), 19→Task 9 (график расхода).

## После MVP-1 (не в этом плане)

Фаза 2: контроллер (расписания часов, дневные лимиты, автостоп) — отдельный спек на основе данных, собранных фазой 1.
