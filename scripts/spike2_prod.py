"""Throwaway diagnostic (read-only): spike2 probe with the DB cabinet token.

Внутри контейнера приложения:
    python scripts/spike2_prod.py

Берёт токен первого активного кабинета из БД (реюз кода probe_from_db) и его
campaign_id из таблицы campaigns, затем делает ТОЛЬКО чтение:
  1) advert-api: GET /api/advert/v0/daily-limits?advertIds=<до 100 id из БД>
  2) statistics-api: GET /api/v1/supplier/orders?dateFrom=<-2 сут, МСК>&flag=0
Печатает markdown (статусы, X-Ratelimit-*, тела с обрезкой, разбор orders).
Токен никогда не печатается. Никаких write-вызовов к WB.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spike2_orders import analyze_orders, call, msk_rfc3339_days_ago
from sqlalchemy import select

from app.crypto import decrypt_token
from app.db import SessionLocal
from app.models import Cabinet, Campaign

ACTIVE_STATUSES = (9, 11)


async def first_active_cabinet() -> tuple[str, int, list[int]]:
    """Токен + id первого активного кабинета и до 100 его campaign_id (сначала активные)."""
    async with SessionLocal() as db:
        cabinet = (
            (await db.execute(select(Cabinet).where(Cabinet.active == True)))
            .scalars()
            .first()
        )
        if cabinet is None:
            sys.exit("no active cabinet in DB")
        campaign_rows = (
            (
                await db.execute(
                    select(Campaign.advert_id, Campaign.status).where(Campaign.cabinet_id == cabinet.id)
                )
            )
            .all()
        )
    active = [int(aid) for aid, status in campaign_rows if status in ACTIVE_STATUSES]
    other = [int(aid) for aid, status in campaign_rows if status not in ACTIVE_STATUSES]
    ids = (active + other)[:100]  # в query идут только целые, максимум 100 (спека)
    return decrypt_token(cabinet.token_encrypted), cabinet.id, ids


def main() -> None:
    token, cabinet_id, advert_ids = asyncio.run(first_active_cabinet())
    print(f"## prod cabinet id={cabinet_id}: {len(advert_ids)} campaign_id из БД"
          f" (первая пятёрка: {advert_ids[:5]})", flush=True)

    if advert_ids:
        call(
            "advert-api.wildberries.ru",
            "GET",
            "/api/advert/v0/daily-limits",
            token,
            query={"advertIds": ",".join(map(str, advert_ids))},
        )
    else:
        print("> campaigns в БД нет — daily-limits пропущен (advertIds обязателен)", flush=True)

    date_from = msk_rfc3339_days_ago(2)
    print(f"> orders: dateFrom={date_from} (окно 2 сут, МСК), flag=0", flush=True)
    payload = call(
        "statistics-api.wildberries.ru",
        "GET",
        "/api/v1/supplier/orders",
        token,
        query={"dateFrom": date_from, "flag": 0},
    )
    try:
        analyze_orders(payload)
    except Exception as e:  # noqa: BLE001
        print(f"exception in analyze_orders: {e!r}", flush=True)


if __name__ == "__main__":
    main()
