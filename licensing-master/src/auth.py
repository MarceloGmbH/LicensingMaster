"""Two auth surfaces (ADR-0013):

* `admin_identity` — the human operator behind Cloudflare Access. Verifies the
  `Cf-Access-Jwt-Assertion` header against Cloudflare's public keys (cached),
  checking `aud` and an optional email allow-list. In a dev environment (no
  `LM_ACCESS_TEAM_DOMAIN` set) it falls back to a plain `X-Dev-Admin-Email`
  header so the portal is usable locally.
* `service_identity` — a product backend calling `/cp/*` with a bearer token
  whose SHA-256 must match a non-revoked `licensing.service_tokens` row.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.config import settings
from src.db import get_read_connection
from src.errors import ForbiddenError, UnauthorizedError
from src.tables import service_tokens

# --- Cloudflare Access -----------------------------------------------------

_JWKS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_JWKS_TTL = 3600.0


def _certs_url() -> str:
    return f"https://{settings.LM_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"


async def _jwks() -> list[dict]:
    now = time.time()
    hit = _JWKS_CACHE.get("k")
    if hit and now - hit[0] < _JWKS_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(_certs_url())
        resp.raise_for_status()
        keys = resp.json().get("keys", [])
    _JWKS_CACHE["k"] = (now, keys)
    return keys


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    email: str


async def admin_identity(
    request: Request,
    cf_assertion: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
    dev_email: str | None = Header(default=None, alias="X-Dev-Admin-Email"),
) -> AdminIdentity:
    if not settings.LM_ACCESS_TEAM_DOMAIN:
        # Dev: trust the header. NEVER reached in production (the setting is set).
        if settings.is_dev and dev_email:
            return AdminIdentity(email=dev_email.strip().lower())
        raise UnauthorizedError(message="Cloudflare Access is not configured")

    token = cf_assertion or request.cookies.get("CF_Authorization")
    if not token:
        raise UnauthorizedError(message="Missing Cloudflare Access assertion")

    try:
        header = jwt.get_unverified_header(token)
        keys = await _jwks()
        jwk = next((k for k in keys if k.get("kid") == header.get("kid")), None)
        if jwk is None:
            raise UnauthorizedError(message="Unknown Access signing key")
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.LM_ACCESS_AUD or None,
            options={"verify_aud": bool(settings.LM_ACCESS_AUD)},
        )
    except UnauthorizedError:
        raise
    except Exception as exc:  # noqa: BLE001 - any JWT failure is a 401
        raise UnauthorizedError(message=f"Invalid Access assertion: {exc}") from exc

    email = str(claims.get("email", "")).strip().lower()
    if not email:
        raise ForbiddenError(message="Access assertion carries no email")
    allow = settings.admin_emails
    if allow and email not in allow:
        raise ForbiddenError(message=f"{email} is not an authorised operator")
    return AdminIdentity(email=email)


# --- Service token (product backends -> /cp/*) ---------------------------


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    service_token_id: int
    product_id: int
    tenant_id: int | None


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def service_identity(
    authorization: str | None = Header(default=None),
    conn: Connection = Depends(get_read_connection),
) -> ServiceIdentity:
    if not authorization or not authorization.lower().startswith("bearer "):
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
        raise ForbiddenError(message="Invalid or revoked service token")
    return ServiceIdentity(
        service_token_id=row["service_token_id"],
        product_id=row["product_id"],
        tenant_id=row["tenant_id"],
    )
