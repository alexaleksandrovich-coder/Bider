"""Throwaway diagnostic: run spike_probe.py with the first active cabinet's token.

Prints only probe output (statuses/ratelimits/bodies) — the token itself is
never printed. Usage (inside the app container):
    python scripts/probe_from_db.py
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.crypto import decrypt_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Cabinet  # noqa: E402


async def first_active_token() -> str:
    async with SessionLocal() as db:
        cabinet = (
            (await db.execute(select(Cabinet).where(Cabinet.active == True)))  # noqa: E712
            .scalars()
            .first()
        )
        if cabinet is None:
            sys.exit("no active cabinet in DB")
        return decrypt_token(cabinet.token_encrypted)


def main() -> None:
    token = asyncio.run(first_active_token())
    probe = Path(__file__).with_name("spike_probe.py")
    subprocess.run([sys.executable, str(probe)], env=dict(os.environ, WB_TOKEN_BY1=token))


if __name__ == "__main__":
    main()
