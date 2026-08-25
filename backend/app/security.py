"""JWT issuing and the auth dependency used by every mutating route."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

settings = get_settings()
bearer = HTTPBearer(auto_error=False)


def create_token(subject: str) -> dict:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expires, "iat": datetime.now(timezone.utc)}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "email": subject,
    }


async def current_actor(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    """Return the acting user's email, used for audit rows.

    With AUTH_REQUIRED=false (local development) an anonymous caller is allowed
    through and attributed to the configured admin address.
    """
    if creds is None or not creds.credentials:
        if settings.auth_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token.",
            )
        return settings.admin_email

    try:
        payload = jwt.decode(
            creds.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except jwt.PyJWTError:
        if settings.auth_required:
            raise HTTPException(status_code=401, detail="Invalid token.")
        return settings.admin_email

    return str(payload.get("sub") or settings.admin_email)


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """bcrypt hash, safe to store in users.password."""
    if not plain or len(plain) < 8:
        raise ValueError("Password must be at least 8 characters.")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
