"""Generic CRUD routes driven by the table registry in entities.py."""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi import Path as PathParam
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session, ping
from .entities import ENTITIES, Entity, get_entity, PRIVATE_TABLES
from .security import bearer, create_token, current_actor, hash_password, verify_password

log = logging.getLogger("matchsystems.api")
settings = get_settings()
router = APIRouter()

MAX_PER_PAGE = 500  # dropdowns request the full company list in one call


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _entity_or_404(name: str) -> Entity:
    ent = get_entity(name)
    if ent is None:
        raise HTTPException(status_code=404, detail=f"Unknown table '{name}'.")
    return ent


def _friendly_db_error(exc: Exception) -> HTTPException:
    """Turn a Postgres constraint failure into something the UI can display."""
    orig = getattr(exc, "orig", exc)
    name = type(orig).__name__
    detail = str(getattr(orig, "detail", "") or "")
    message = str(orig).split("\n")[0]

    if "UniqueViolation" in name:
        return HTTPException(status_code=409, detail=detail or "That value already exists.")
    if "ForeignKeyViolation" in name:
        return HTTPException(status_code=422, detail=detail or "Referenced record does not exist.")
    if "CheckViolation" in name:
        return HTTPException(status_code=422, detail=detail or f"Value rejected by a database constraint: {message}")
    if "NotNullViolation" in name:
        return HTTPException(status_code=422, detail=detail or "A required column was left empty.")
    if "InvalidTextRepresentation" in name or "DataError" in name:
        return HTTPException(status_code=422, detail=f"Invalid value for a column: {message}")
    log.exception("Unhandled database error")
    return HTTPException(status_code=500, detail="Database error.")


async def _resolve_references(
    ent: Entity, payload: Dict[str, Any], session: AsyncSession
) -> Dict[str, Any]:
    """Replace natural keys (company_name, client_site) with their foreign keys."""
    resolved: Dict[str, Any] = {}
    for key, ref in ent.references.items():
        if key not in payload:
            continue
        value = payload.pop(key)
        if value in (None, ""):
            continue
        sql = text(
            f"SELECT id FROM {ref.table} WHERE {ref.lookup_column} = :value"  # noqa: S608 - registry-controlled
        )
        found = (await session.execute(sql, {"value": value})).scalar_one_or_none()
        if found is None:
            raise HTTPException(
                status_code=422,
                detail=f"No {ref.table} row with {ref.lookup_column} = '{value}'.",
            )
        resolved[ref.fk_column] = found
    return resolved


def _apply_aliases(ent: Entity, body: Dict[str, Any]) -> Dict[str, Any]:
    """Rename the UI's friendly payload keys onto real column names."""
    return {ent.aliases.get(k, k): v for k, v in body.items()}


def _apply_hashes(ent: Entity, body: Dict[str, Any]) -> Dict[str, Any]:
    """Hash plaintext secrets into their storage columns."""
    out = dict(body)
    for plain_key, column in ent.hash_fields.items():
        if plain_key not in out:
            continue
        raw = str(out.pop(plain_key) or "")
        if not raw:
            continue
        try:
            out[column] = hash_password(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return out


def _strip_hidden(ent: Entity, row: Dict[str, Any]) -> Dict[str, Any]:
    """Drop columns that must never leave the server."""
    if not ent.hidden:
        return row
    return {k: v for k, v in row.items() if k not in ent.hidden}


def _clean_payload(ent: Entity, body: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only columns this table declares writable."""
    return {k: v for k, v in body.items() if k in ent.writable}


async def _write_audit(
    session: AsyncSession, actor: str, action: str, entity: str, target: str, level: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO logs (actor, action, entity, target, level) "
            "VALUES (:actor, :action, :entity, :target, :level)"
        ),
        {"actor": actor, "action": action, "entity": entity, "target": str(target or ""), "level": level},
    )


# ---------------------------------------------------------------------------
# file upload
# ---------------------------------------------------------------------------
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


def _safe_stem(name: str) -> str:
    """A short, predictable slug from the original filename."""
    stem = Path(name or "image").stem.lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    return (cleaned or "image")[:40]


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    prefix: str = Form("upload"),
    actor: str = Depends(current_actor),
) -> dict:
    """Store an uploaded image on disk and return its relative URL.

    Only the returned path is written to the database; the binary never goes
    into Postgres. Base64 in a text column inflates every row and every API
    response that touches it.
    """
    ext = ALLOWED_IMAGE_TYPES.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{file.content_type}'. Allowed: PNG, JPEG, WebP, GIF, SVG.",
        )

    limit = settings.max_upload_mb * 1024 * 1024
    body = await file.read(limit + 1)
    if len(body) > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB.")
    if not body:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")

    target_dir = settings.upload_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "_", (prefix or "upload").lower()).strip("_") or "upload"
    name = f"{slug}_{_safe_stem(file.filename)}_{uuid.uuid4().hex[:8]}{ext}"
    path = target_dir / name

    try:
        path.write_bytes(body)
    except OSError as exc:
        log.exception("Could not write upload to %s", path)
        raise HTTPException(status_code=500, detail="Could not save the file on the server.") from exc

    url = f"/uploads/{name}"
    log.info("upload by %s -> %s (%d bytes)", actor, url, len(body))
    return {"url": url, "filename": name, "size": len(body), "content_type": file.content_type}

# ---------------------------------------------------------------------------
# health + auth
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict:
    reachable = await ping()
    if not reachable:
        raise HTTPException(status_code=503, detail="Database is not reachable.")
    return {"status": "ok", "database": settings.pg_database, "tables": sorted(ENTITIES)}


@router.post("/token")
@router.post("/auth/login")
async def login(
    body: Dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Authenticate against the users table. No hardcoded credentials."""
    email = str(body.get("email") or body.get("username") or "").strip().lower()
    password = str(body.get("password", ""))
    if not email or not password:
        raise HTTPException(status_code=422, detail="Email and password are required.")

    row = (
        await session.execute(
            text("SELECT id, name, email, password FROM users WHERE lower(email) = :email"),
            {"email": email},
        )
    ).mappings().one_or_none()

    # Same message either way: never reveal whether the address exists.
    if row is None or not verify_password(password, row["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    await _write_audit(session, row["email"], "login", "users", row["email"], "active")
    await session.commit()

    token = create_token(row["email"])
    token["name"] = row["name"]
    return token


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# demo requests
# ---------------------------------------------------------------------------
DEMO_SECTORS = {"education", "distribution", "health", "accounting", "inventory", "hr", "general", ""}
DEMO_STATUSES = ("pending", "contacted", "completed", "cancelled")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _clean(value, limit: int) -> str:
    return (str(value or "").strip())[:limit]


@router.post("/demo-requests", status_code=201)
async def create_demo_request(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Public: accept a Book-a-Demo submission.

    Unauthenticated, so nothing from the payload is trusted. Only the six
    known fields are read, each is length-capped, and status is never taken
    from the caller - a new lead is always 'pending'.
    """
    full_name = _clean(payload.get("full_name") or payload.get("name"), 120)
    email = _clean(payload.get("email"), 160).lower()
    phone = _clean(payload.get("phone_number") or payload.get("phone"), 32)
    company = _clean(payload.get("company_name") or payload.get("company"), 160)
    sector = _clean(payload.get("sector"), 40).lower()
    message = _clean(payload.get("message"), 4000)

    errors = []
    if len(full_name) < 2:
        errors.append({"key": "full_name", "message": "Full name is required."})
    if not EMAIL_RE.match(email):
        errors.append({"key": "email", "message": "A valid e-mail address is required."})
    if len(phone) < 5:
        errors.append({"key": "phone_number", "message": "A phone number is required."})
    if sector and sector not in DEMO_SECTORS:
        errors.append({"key": "sector", "message": "Unknown sector."})
    if errors:
        raise HTTPException(status_code=422, detail={"message": "Invalid submission.", "fields": errors})

    row = (await session.execute(
        text("""
            INSERT INTO demo_requests (full_name, email, phone_number, company_name, sector, message)
            VALUES (:full_name, :email, :phone, :company, :sector, :message)
            RETURNING id, created_at
        """),
        {"full_name": full_name, "email": email, "phone": phone,
         "company": company or None, "sector": sector or None, "message": message or None},
    )).mappings().first()
    await session.commit()

    log.info("demo request %s from %s", row["id"], email)
    # Deliberately thin: the public form gets an acknowledgement, not the row.
    return {"id": row["id"], "status": "pending", "created_at": row["created_at"]}


@router.get("/demo-requests")
async def list_demo_requests(
    q: str = Query("", description="Search name, e-mail, phone, company or message"),
    page: int = Query(0, ge=0),
    per: int = Query(20, ge=1, le=MAX_PER_PAGE),
    status: str = Query("", description="Filter by status"),
    sort: str = Query("created_at"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    actor: str = Depends(current_actor),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Authenticated: newest first by default."""
    ent = ENTITIES["demo_requests"]
    column = sort if sort in ent.sortable else "created_at"
    where, params = [], {}
    if status:
        if status not in DEMO_STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown status '{status}'.")
        where.append("status = :status")
        params["status"] = status
    if q:
        where.append("(" + " OR ".join(f"{c} ILIKE :q" for c in ent.search) + ")")
        params["q"] = f"%{q}%"
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = (await session.execute(
        text(f"SELECT count(*) FROM demo_requests{clause}"), params)).scalar_one()
    params.update({"limit": per, "offset": page * per})
    rows = (await session.execute(
        text(f"SELECT * FROM demo_requests{clause} "
             f"ORDER BY {column} {dir.upper()}, id DESC LIMIT :limit OFFSET :offset"),
        params)).mappings().all()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per": per}


@router.patch("/demo-requests/{row_id}")
async def update_demo_request(
    row_id: int = PathParam(..., ge=1),
    payload: dict = Body(...),
    actor: str = Depends(current_actor),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Authenticated: move a lead through the pipeline.

    Status is the only mutable field - the contact details are the customer's
    own words and must not be edited from the dashboard.
    """
    status = _clean(payload.get("status"), 20).lower()
    if status not in DEMO_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(DEMO_STATUSES)}.")

    row = (await session.execute(
        text("""UPDATE demo_requests SET status = CAST(:status AS demo_status), handled_by = :actor
                WHERE id = :id RETURNING *"""),
        {"status": status, "actor": actor, "id": row_id},
    )).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No demo request with id {row_id}.")
    await session.commit()
    log.info("demo request %s -> %s by %s", row_id, status, actor)
    return dict(row)


@router.get("/{table}")
async def list_rows(
    table: str = PathParam(...),
    q: str = Query("", description="Free-text search across the table's text columns"),
    page: int = Query(0, ge=0),
    per: int = Query(8, ge=1, le=MAX_PER_PAGE),
    sort: str = Query("id"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
    sector: str = Query("", description="Filter by sector column when the table has one"),
    status: str = Query("", description="Filter by status column"),
    level: str = Query("", description="Filter logs by level"),
    is_visible: str = Query("", description="Filter by the visibility flag (true/false)"),
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    ent = _entity_or_404(table)
    if table in PRIVATE_TABLES:
        # This route is public so the landing pages can read clients, feedback
        # and stats. Tables holding personal data stay on it for the admin, but
        # only for a caller that presents a valid token.
        await current_actor(creds)

    sort_column = sort if sort in ent.sortable else "id"
    direction = "DESC" if dir.lower() == "desc" else "ASC"
    params: Dict[str, Any] = {"limit": per, "offset": page * per}

    clauses = []
    if q.strip() and ent.search:
        clauses.append("(" + " OR ".join(f"{ent.table}.{col}::text ILIKE :q" for col in ent.search) + ")")
        params["q"] = f"%{q.strip()}%"

    for name, value in (("sector", sector), ("status", status), ("level", level), ("is_visible", is_visible)):
        if value.strip() and name in ent.filterable:
            clauses.append(f"{ent.table}.{name}::text = :f_{name}")
            params[f"f_{name}"] = value.strip()

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    count_sql = text(f"SELECT count(*) FROM {ent.table} {ent.join_clause} {where}")  # noqa: S608
    rows_sql = text(
        f"SELECT {ent.select_columns} FROM {ent.table} {ent.join_clause} {where} "  # noqa: S608
        f"ORDER BY {ent.table}.{sort_column} {direction}, {ent.table}.id {direction} "
        f"LIMIT :limit OFFSET :offset"
    )

    try:
        total = (await session.execute(count_sql, params)).scalar_one()
        result = await session.execute(rows_sql, params)
    except (IntegrityError, DBAPIError) as exc:
        raise _friendly_db_error(exc) from exc

    items = [_strip_hidden(ent, dict(r)) for r in result.mappings().all()]
    return {"items": items, "total": total, "page": page, "per": per}


@router.post("/{table}", status_code=201)
async def create_row(
    table: str = PathParam(...),
    body: Dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(current_actor),
) -> dict:
    ent = _entity_or_404(table)
    if ent.read_only:
        raise HTTPException(status_code=405, detail=f"'{table}' is read only.")

    payload = _apply_hashes(ent, _apply_aliases(ent, dict(body)))
    fks = await _resolve_references(ent, payload, session)
    values = _clean_payload(ent, payload)
    values.update(fks)
    if ent.stamps_creator:
        values["created_by"] = actor

    if not values:
        raise HTTPException(status_code=422, detail="No writable columns in the request body.")

    if ent.table == "users" and "password" not in values:
        raise HTTPException(status_code=422, detail="A password is required for a new user.")

    columns = ", ".join(values)
    binds = ", ".join(f":{c}" for c in values)
    sql = text(f"INSERT INTO {ent.table} ({columns}) VALUES ({binds}) RETURNING *")  # noqa: S608

    try:
        row = (await session.execute(sql, values)).mappings().one()
        await _write_audit(session, actor, "create", ent.table, row.get(ent.label_column), "active")
        await session.commit()
    except (IntegrityError, DBAPIError) as exc:
        await session.rollback()
        raise _friendly_db_error(exc) from exc

    return _strip_hidden(ent, dict(row))


@router.put("/{table}/{row_id}")
@router.patch("/{table}/{row_id}")
async def update_row(
    table: str = PathParam(...),
    row_id: int = PathParam(..., ge=1),
    body: Dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(current_actor),
) -> dict:
    ent = _entity_or_404(table)
    if ent.read_only:
        raise HTTPException(status_code=405, detail=f"'{table}' is read only.")

    payload = _apply_hashes(ent, _apply_aliases(ent, dict(body)))
    fks = await _resolve_references(ent, payload, session)
    values = _clean_payload(ent, payload)
    values.update(fks)

    if not values:
        raise HTTPException(status_code=422, detail="Nothing to update.")

    assignments = ", ".join(f"{c} = :{c}" for c in values)
    values["row_id"] = row_id
    sql = text(f"UPDATE {ent.table} SET {assignments} WHERE id = :row_id RETURNING *")  # noqa: S608

    try:
        row = (await session.execute(sql, values)).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Record not found.")
        await _write_audit(session, actor, "update", ent.table, row.get(ent.label_column), "draft")
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except (IntegrityError, DBAPIError) as exc:
        await session.rollback()
        raise _friendly_db_error(exc) from exc

    return _strip_hidden(ent, dict(row))


@router.delete("/{table}/{row_id}")
async def delete_row(
    table: str = PathParam(...),
    row_id: int = PathParam(..., ge=1),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(current_actor),
) -> dict:
    ent = _entity_or_404(table)

    sql = text(f"DELETE FROM {ent.table} WHERE id = :row_id RETURNING *")  # noqa: S608
    try:
        row = (await session.execute(sql, {"row_id": row_id})).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Record not found.")
        if ent.table != "logs":
            await _write_audit(session, actor, "delete", ent.table, row.get(ent.label_column), "suspended")
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except (IntegrityError, DBAPIError) as exc:
        await session.rollback()
        raise _friendly_db_error(exc) from exc

    return {"ok": True, "id": row_id}
