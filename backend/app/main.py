"""FastAPI entry point.

    cd backend
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .config import get_settings
from .db import engine, init_database, ping
from .routes import router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("matchsystems")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if await ping():
        log.info("Connected to postgres://%s:%s/%s", settings.pg_host, settings.pg_port, settings.pg_database)
        try:
            await init_database()
        except Exception:  # noqa: BLE001 - never block startup on migration failure
            log.exception("Schema migration failed - the API will still start")
    else:
        log.error(
            "Cannot reach postgres://%s:%s/%s - check backend/.env",
            settings.pg_host, settings.pg_port, settings.pg_database,
        )
    yield
    await engine.dispose()


app = FastAPI(
    title="Match Systems Admin API",
    version="1.0.0",
    description="CRUD API backing the Match Systems admin panel.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    """Uniform error envelope: the admin panel reads `message`."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail, "status": exc.status_code},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc: RequestValidationError):
    errors = [
        {"key": ".".join(str(p) for p in e.get("loc", [])[1:]), "message": e.get("msg", "Invalid value")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "message": errors[0]["message"] if errors else "Validation failed.",
                "errors": errors,
                "status": 422,
            }
        ),
    )


@app.get("/")
async def root() -> dict:
    return {"service": "Match Systems Admin API", "prefix": settings.api_prefix, "docs": "/docs"}


app.include_router(router, prefix=settings.api_prefix)
