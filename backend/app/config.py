"""Settings loaded from backend/.env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
SQL_DIR = PROJECT_DIR / "db"

load_dotenv(BACKEND_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.pg_host = os.getenv("PGHOST", "localhost")
        self.pg_port = int(os.getenv("PGPORT", "5432"))
        self.pg_database = os.getenv("PGDATABASE", "matchsystems")
        self.pg_user = os.getenv("PGUSER", "postgres")
        self.pg_password = os.getenv("PGPASSWORD", "")

        self.api_port = int(os.getenv("API_PORT", "8000"))
        self.api_prefix = os.getenv("API_PREFIX", "/v1").rstrip("/")
        self.cors_origins = [
            o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
        ]

        self.auth_required = _bool("AUTH_REQUIRED", True)
        self.jwt_secret = os.getenv("JWT_SECRET", "dev-secret-change-me")
        self.jwt_algorithm = os.getenv("JWT_ALGORITHM", "HS256")
        self.jwt_expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
        self.admin_email = os.getenv("ADMIN_EMAIL", "admin@matchsystems.com")
        # No login password here: credentials live in the users table.

        self.auto_migrate = _bool("AUTO_MIGRATE", True)

        self.schema_sql = SQL_DIR / "schema.sql"

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+asyncpg://{quote_plus(self.pg_user)}:{quote_plus(self.pg_password)}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def asyncpg_dsn(self) -> str:
        return (
            f"postgresql://{quote_plus(self.pg_user)}:{quote_plus(self.pg_password)}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
