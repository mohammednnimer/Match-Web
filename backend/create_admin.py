"""Create (or update) an admin account in the users table.

Login validates only against this table, so run this once after the schema is
in place -- otherwise nobody can sign in.

    cd backend
    python create_admin.py                          # prompts for the details
    python create_admin.py admin@example.com "Site Admin" "s3cret-pass"

Re-running with an existing email resets that user's password and reactivates
the account.
"""
from __future__ import annotations

import asyncio
import getpass
import sys

import asyncpg

from app.config import get_settings
from app.security import hash_password


async def main() -> int:
    settings = get_settings()
    args = sys.argv[1:]

    email = (args[0] if len(args) > 0 else input("Email: ")).strip().lower()
    name = (args[1] if len(args) > 1 else input("Display name: ")).strip()
    password = args[2] if len(args) > 2 else getpass.getpass("Password (min 8 chars): ")

    if not email or "@" not in email:
        print("ERROR a valid email address is required")
        return 1
    if len(name) < 2:
        print("ERROR name must be at least 2 characters")
        return 1

    try:
        hashed = hash_password(password)
    except ValueError as exc:
        print(f"ERROR {exc}")
        return 1

    try:
        conn = await asyncpg.connect(dsn=settings.asyncpg_dsn, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR cannot reach the database: {exc}")
        print("      check backend/.env, and that init_db.py has been run")
        return 1

    try:
        existing = await conn.fetchrow("SELECT id FROM users WHERE lower(email) = $1", email)
        if existing:
            await conn.execute(
                "UPDATE users SET name = $2, password = $3 WHERE id = $1",
                existing["id"], name, hashed,
            )
            print(f"updated existing admin: {email}")
        else:
            await conn.execute(
                "INSERT INTO users (name, email, password) VALUES ($1, $2, $3)",
                name, email, hashed,
            )
            print(f"created admin: {email}")
    except asyncpg.UndefinedTableError:
        print("ERROR the users table does not exist - run: python init_db.py")
        return 1
    finally:
        await conn.close()

    print("You can now sign in from the admin panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
