"""Generic CRUD routes driven by the table registry in entities.py."""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session, ping
from .entities import ENTITIES, Entity, get_entity
from .security import create_token, current_actor, hash_password, verify_password

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
@router.get("/{table}")
async def list_rows(
    table: str = Path(...),
    q: str = Query("", description="Free-text search across the table's text columns"),
    page: int = Query(0, ge=0),
    per: int = Query(8, ge=1, le=MAX_PER_PAGE),
    sort: str = Query("id"),
    dir: str = Query("asc", pattern="^(asc|desc)$"),
    sector: str = Query("", description="Filter by sector column when the table has one"),
    status: str = Query("", description="Filter by status column"),
    level: str = Query("", description="Filter logs by level"),
    is_visible: str = Query("", description="Filter by the visibility flag (true/false)"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    ent = _entity_or_404(table)

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
    table: str = Path(...),
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
    table: str = Path(...),
    row_id: int = Path(..., ge=1),
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
    table: str = Path(...),
    row_id: int = Path(..., ge=1),
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
