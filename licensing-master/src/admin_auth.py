"""Native admin authentication: argon2id passwords, mandatory TOTP, server-side sessions.

* Passwords: argon2id (``argon2-cffi``). A dummy hash is verified when the e-mail
  is unknown so a failed login costs about the same either way.
* TOTP: RFC 6238 (30 s, 6 digits, +-1 step of drift). The last accepted time step
  is stored per user, so a code can be used once.
* Sessions: an opaque 256-bit random token in the ``lm_session`` cookie; only its
  sha256 is stored. Idle timeout slides, absolute lifetime does not.

The same functions back both the HTTP endpoints and the ``python -m src.cli``
operator commands.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Connection

from src.config import settings
from src.service import _audit
from src.tables import admin_sessions, admin_users

SESSION_COOKIE = "lm_session"
TOTP_ISSUER = "Licensing Master"
TOTP_INTERVAL = 30
MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 1024  # cap work an attacker can make us hash
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_hasher = PasswordHasher()  # argon2id with the library's current default cost


class AdminUserError(Exception):
    """An operator-facing problem with an admin account (bad input, not found...)."""


def utcnow() -> datetime:
    """Naive UTC, matching the rest of the schema (TIMESTAMP without time zone)."""
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_email(email: str) -> str:
    return email.strip().lower()


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > _MAX_PASSWORD_LENGTH:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@functools.cache
def _dummy_hash() -> str:
    return hash_password("dummy password used only to equalise login timing")


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AdminUserError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise AdminUserError(f"Password must be at most {_MAX_PASSWORD_LENGTH} characters long")


# --- TOTP --------------------------------------------------------------------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def match_totp_step(secret: str, code: str) -> int | None:
    """The time step ``code`` is valid for (current step +-1), or None."""
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time() // TOTP_INTERVAL)
    found: int | None = None
    for step in (current - 1, current, current + 1):  # no early exit: constant work
        if hmac.compare_digest(totp.at(step * TOTP_INTERVAL), code) and found is None:
            found = step
    return found


# --- account management (CLI) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedAdmin:
    email: str
    totp_secret: str
    otpauth_uri: str


def _user_row(conn: Connection, email: str):
    row = (
        conn.execute(select(admin_users).where(admin_users.c.email == normalize_email(email)))
        .mappings()
        .first()
    )
    if row is None:
        raise AdminUserError(f"Admin {normalize_email(email)} not found")
    return row


def create_admin(conn: Connection, email: str, password: str, *, actor: str = "cli") -> CreatedAdmin:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email) or len(email) > 180:
        raise AdminUserError("A valid e-mail address is required")
    validate_password_strength(password)
    if conn.execute(select(admin_users.c.admin_user_id).where(admin_users.c.email == email)).first():
        raise AdminUserError(f"Admin {email} already exists")
    secret = new_totp_secret()
    conn.execute(
        admin_users.insert().values(
            email=email, password_hash=hash_password(password), totp_secret=secret
        )
    )
    _audit(conn, actor, "admin.create", "admin_user", email)
    return CreatedAdmin(email=email, totp_secret=secret, otpauth_uri=provisioning_uri(secret, email))


def revoke_user_sessions(conn: Connection, admin_user_id: int) -> int:
    res = conn.execute(
        update(admin_sessions)
        .where(admin_sessions.c.admin_user_id == admin_user_id, admin_sessions.c.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return res.rowcount


def reset_password(conn: Connection, email: str, new_password: str, *, actor: str = "cli") -> None:
    row = _user_row(conn, email)
    validate_password_strength(new_password)
    conn.execute(
        update(admin_users)
        .where(admin_users.c.admin_user_id == row["admin_user_id"])
        .values(password_hash=hash_password(new_password))
    )
    revoke_user_sessions(conn, row["admin_user_id"])
    _audit(conn, actor, "admin.reset_password", "admin_user", row["email"])


def reset_totp(conn: Connection, email: str, *, actor: str = "cli") -> CreatedAdmin:
    row = _user_row(conn, email)
    secret = new_totp_secret()
    conn.execute(
        update(admin_users)
        .where(admin_users.c.admin_user_id == row["admin_user_id"])
        .values(totp_secret=secret, totp_last_step=None)
    )
    revoke_user_sessions(conn, row["admin_user_id"])
    _audit(conn, actor, "admin.reset_totp", "admin_user", row["email"])
    return CreatedAdmin(email=row["email"], totp_secret=secret, otpauth_uri=provisioning_uri(secret, row["email"]))


def set_active(conn: Connection, email: str, active: bool, *, actor: str = "cli") -> None:
    row = _user_row(conn, email)
    conn.execute(
        update(admin_users).where(admin_users.c.admin_user_id == row["admin_user_id"]).values(is_active=active)
    )
    if not active:
        revoke_user_sessions(conn, row["admin_user_id"])
    _audit(conn, actor, "admin.enable" if active else "admin.disable", "admin_user", row["email"])


def revoke_sessions(conn: Connection, email: str, *, actor: str = "cli") -> int:
    row = _user_row(conn, email)
    n = revoke_user_sessions(conn, row["admin_user_id"])
    _audit(conn, actor, "admin.revoke_sessions", "admin_user", row["email"], {"count": n})
    return n


def list_admins(conn: Connection) -> list[dict]:
    """Admin accounts WITHOUT secrets (no hash, no TOTP secret)."""
    rows = conn.execute(
        select(
            admin_users.c.email,
            admin_users.c.is_active,
            admin_users.c.created_at,
            admin_users.c.last_login_at,
        ).order_by(admin_users.c.admin_user_id)
    ).mappings()
    return [dict(r) for r in rows]


# --- sessions ----------------------------------------------------------------


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(conn: Connection, admin_user_id: int, *, ip: str | None, user_agent: str | None) -> str:
    """Insert a session and return the RAW token (only its hash is stored)."""
    raw = secrets.token_urlsafe(32)  # 256 bits
    now = utcnow()
    # opportunistic housekeeping: forget sessions that expired a day ago
    conn.execute(delete(admin_sessions).where(admin_sessions.c.expires_at < now - timedelta(days=1)))
    conn.execute(
        admin_sessions.insert().values(
            admin_user_id=admin_user_id,
            token_hash=token_hash(raw),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=settings.LM_ADMIN_SESSION_ABSOLUTE_SECONDS),
            ip=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    return raw


def resolve_session(conn: Connection, raw: str) -> str | None:
    """Validate a cookie value; return the admin e-mail (and slide the idle timer) or None."""
    row = (
        conn.execute(
            select(
                admin_sessions.c.admin_session_id,
                admin_sessions.c.last_seen_at,
                admin_sessions.c.expires_at,
                admin_sessions.c.revoked_at,
                admin_users.c.email,
                admin_users.c.is_active,
            )
            .join(admin_users, admin_users.c.admin_user_id == admin_sessions.c.admin_user_id)
            .where(admin_sessions.c.token_hash == token_hash(raw))
        )
        .mappings()
        .first()
    )
    if row is None or row["revoked_at"] is not None or not row["is_active"]:
        return None
    now = utcnow()
    if row["expires_at"] <= now:
        return None
    if now - row["last_seen_at"] > timedelta(seconds=settings.LM_ADMIN_SESSION_IDLE_SECONDS):
        return None
    conn.execute(
        update(admin_sessions)
        .where(admin_sessions.c.admin_session_id == row["admin_session_id"])
        .values(last_seen_at=now)
    )
    return row["email"]


def revoke_session(conn: Connection, raw: str) -> str | None:
    """Revoke the session behind ``raw``; return its owner's e-mail if one was live."""
    row = (
        conn.execute(
            select(admin_sessions.c.admin_session_id, admin_users.c.email)
            .join(admin_users, admin_users.c.admin_user_id == admin_sessions.c.admin_user_id)
            .where(admin_sessions.c.token_hash == token_hash(raw), admin_sessions.c.revoked_at.is_(None))
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    conn.execute(
        update(admin_sessions)
        .where(admin_sessions.c.admin_session_id == row["admin_session_id"])
        .values(revoked_at=utcnow())
    )
    return row["email"]


# --- login -------------------------------------------------------------------


def login(
    conn: Connection, email: str, password: str, totp_code: str, *, ip: str | None, user_agent: str | None
) -> tuple[str, str] | None:
    """Verify credentials; on success return ``(raw_session_token, email)``.

    Any failure returns None (callers must answer with ONE generic error) and is
    audited without the password or the code. Both factors are always checked,
    against dummies when the user is unknown, so timing reveals little.
    """
    email = normalize_email(email)[:180]
    user = conn.execute(select(admin_users).where(admin_users.c.email == email)).mappings().first()

    password_ok = verify_password(user["password_hash"] if user else _dummy_hash(), password)
    step = match_totp_step(user["totp_secret"] if user else pyotp.random_base32(), totp_code)

    if user is not None and user["is_active"] and password_ok and step is not None:
        # Consume the time step atomically: a code (or an older one) can't be reused.
        used = conn.execute(
            update(admin_users)
            .where(
                admin_users.c.admin_user_id == user["admin_user_id"],
                (admin_users.c.totp_last_step.is_(None)) | (admin_users.c.totp_last_step < step),
            )
            .values(totp_last_step=step, last_login_at=utcnow())
        )
        if used.rowcount == 1:
            raw = create_session(conn, user["admin_user_id"], ip=ip, user_agent=user_agent)
            _audit(conn, email, "admin.login", "admin_user", email, {"ip": ip})
            return raw, email

    _audit(conn, email, "admin.login_failed", "admin_user", email, {"ip": ip})
    return None


def verify_admin_password(conn: Connection, email: str, password: str) -> bool:
    """Re-authentication for dangerous actions: the caller's OWN password."""
    row = conn.execute(
        select(admin_users.c.password_hash).where(
            admin_users.c.email == normalize_email(email), admin_users.c.is_active.is_(True)
        )
    ).first()
    return bool(row) and verify_password(row[0], password)
