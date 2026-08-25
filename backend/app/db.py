"""Async engine, session factory, and the SQL-file runner used at startup."""
from __future__ import annotations

import logging
from typing import AsyncIterator

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

log = logging.getLogger("matchsystems.db")
settings = get_settings()

engine = create_async_engine(
    settings.sqlalchemy_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def run_sql_file(path) -> None:
    """Execute a multi-statement .sql file.

    asyncpg's simple-query protocol is used deliberately: SQLAlchemy's text()
    cannot execute several statements (or dollar-quoted PL/pgSQL bodies) in one
    round trip, and db/schema.sql relies on both.
    """
    if not path.exists():
        log.warning("SQL file not found, skipping: %s", path)
        return

    sql = path.read_text(encoding="utf-8")
    conn = await asyncpg.connect(dsn=settings.asyncpg_dsn)
    try:
        await conn.execute(sql)
        log.info("Executed %s", path.name)
    finally:
        await conn.close()


async def init_database() -> None:
    if settings.auto_migrate:
        await run_sql_file(settings.schema_sql)


async def ping() -> bool:
    try:
        conn = await asyncpg.connect(dsn=settings.asyncpg_dsn, timeout=5)
    except Exception:  # noqa: BLE001 - any connection failure means "not reachable"
        return False
    try:
        await conn.fetchval("SELECT 1")
        return True
    finally:
        await conn.close()
