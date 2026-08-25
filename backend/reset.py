import asyncio, asyncpg
from app.config import get_settings

async def main():
    s = get_settings()
    url = str(s.asyncpg_dsn).replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    print("Database reset successfully!")

asyncio.run(main())
