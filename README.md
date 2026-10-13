# Bider

Read-only foundation of a Wildberries autobidder: background asyncio loops collect
campaigns, bids, budgets, balances, stats and auction snapshots from the official
WB advert-api into PostgreSQL, plus a login-protected dashboard. Spec: `.scratch/spec.md`.

## Dev quick start (Windows / Git Bash)

- `python -m venv .venv && .venv/Scripts/python -m pip install -e . --group dev`
- `cp .env.example .env` and fill in values (Fernet key command is inside)
- `docker compose -f docker-compose.dev.yml up -d` — dev PostgreSQL on localhost:5436
- `.venv/Scripts/python -m pytest -v` — run tests

## БД (database)

- Lift dev Postgres: `docker compose -f docker-compose.dev.yml up -d postgres`
  (user `app` / password `app` / db `bider`, host port **5436**; wait for `healthy`).
- Apply migrations: `.venv/Scripts/python -m alembic upgrade head`
  (the URL is taken from `BIDER_DATABASE_URL` / `.env`).
- Tests use a separate database, isolated from dev data: `BIDER_TEST_DATABASE_URL`
  (default `postgresql+asyncpg://app:app@localhost:5436/bider_test`; the test suite
  creates it automatically if missing).
