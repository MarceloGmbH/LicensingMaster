"""Two auth surfaces (ADR-0013):

* `admin_identity` — the human operator, logged in natively (password + TOTP,
  see `admin_auth`) and carried by the HttpOnly `lm_session` cookie.
* `service_identity` — a product backend calling `/cp/*` with a bearer token
  whose SHA-256 must match a non-revoked `licensing.service_tokens` row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.db import get_read_connection
from src import admin_auth, ratelimit
from src.errors import ForbiddenError, TooManyAttemptsError, UnauthorizedError
from src.tables import service_tokens

# --- Admin session (portal -> /admin/*) -------------------------------------


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    email: str


def admin_identity(
    request: Request,
    conn: Connection = Depends(get_read_connection),
) -> AdminIdentity:
    """The logged-in operator, from the `lm_session` cookie. The session must
    exist, be unrevoked, inside its idle + absolute lifetime, and belong to an
    active user; a valid request slides the idle timer."""
    raw = request.cookies.get(admin_auth.SESSION_COOKIE)
    email = admin_auth.resolve_session(conn, raw) if raw else None
    conn.commit()  # persist the sliding last_seen_at
    if email is None:
        raise UnauthorizedError()
    return AdminIdentity(email=email)


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "lm-portal"


def csrf_guard(request: Request) -> None:
    """Defence in depth next to SameSite=Strict: every state-changing /admin/*
    request must carry the header only the portal's own fetch wrapper sends
    (a cross-site form post cannot set it, and a cross-origin fetch with it
    needs a CORS preflight)."""
    if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise ForbiddenError(message=f"Missing or invalid {CSRF_HEADER} header")


# --- Service token (product backends -> /cp/*) ---------------------------


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    service_token_id: int
    product_id: int
    tenant_id: int | None


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def service_identity(
    request: Request,
    authorization: str | None = Header(default=None),
    conn: Connection = Depends(get_read_connection),
) -> ServiceIdentity:
    ip = ratelimit.client_ip(request)
    wait = ratelimit.service_auth_limiter.retry_after(ip)
    if wait:
        raise TooManyAttemptsError(headers={"Retry-After": str(wait)})
    if not authorization or not authorization.lower().startswith("bearer "):
        ratelimit.service_auth_limiter.record_failure(ip)
        raise UnauthorizedError(message="Missing service token")
    raw = authorization.split(" ", 1)[1].strip()
    row = (
        conn.execute(
            select(service_tokens).where(
                service_tokens.c.token_hash == _hash(raw),
                service_tokens.c.is_revoked.is_(False),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        ratelimit.service_auth_limiter.record_failure(ip)
        raise ForbiddenError(message="Invalid or revoked service token")
    return ServiceIdentity(
        service_token_id=row["service_token_id"],
        product_id=row["product_id"],
        tenant_id=row["tenant_id"],
    )
