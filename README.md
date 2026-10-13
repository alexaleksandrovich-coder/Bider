# Bider

Read-only foundation of a Wildberries autobidder: background asyncio loops collect
campaigns, bids, budgets, balances, stats and auction snapshots from the official
WB advert-api into PostgreSQL, plus a login-protected dashboard. Spec: `.scratch/spec.md`.

## Dev quick start (Windows / Git Bash)

- `python -m venv .venv && .venv/Scripts/python -m pip install -e . --group dev`
- `cp .env.example .env` and fill in values (Fernet key command is inside)
- `docker compose -f docker-compose.dev.yml up -d` — dev PostgreSQL on localhost:5436
- `.venv/Scripts/python -m pytest -v` — run tests
