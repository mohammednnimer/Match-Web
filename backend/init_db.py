"""Schema runner.

Creates/updates the tables from db/schema.sql. It does NOT insert any records:
the database starts empty and is filled through the admin panel.

    cd backend
    python init_db.py
"""
from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db import ping, run_sql_file


async def main() -> int:
    settings = get_settings()

    if not await ping():
        print(f"ERROR cannot reach postgres://{settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
        print("      check backend/.env, and that the database exists")
        return 1

    print(f"running {settings.schema_sql}")
    await run_sql_file(settings.schema_sql)
    print("schema is up to date - no records inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
