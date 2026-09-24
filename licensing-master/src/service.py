"""Use cases for licensing-master (ADR-0013) — plain functions over a Connection.

Two audiences: `admin_*` (portal, Cloudflare Access) and `cp_*` (product
backends, service token). The `cp_*` set mirrors the pharmacy `LicensingControlPlane`
port so the stub becomes a drop-in HTTP client.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from src.config import settings
from src.errors import (
    ActivationTokenInvalidError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    SeatLimitReachedError,
    ValidationError,
)
from src.signer import sign_payload
from src.tables import (
    audit_log,
    batch_tokens,
    device_activations,
    payments,
    products,
    service_tokens,
    subscriptions,
    tenants,
)

_STATE_ACTIVE = "ACTIVE"
_STATE_GRACE = "GRACE"
_STATE_EXPIRED = "EXPIRED"
_STATE_REVOKED = "REVOKED"
_STATE_UNKNOWN = "UNKNOWN"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _audit(conn: Connection, actor: str, action: str, target_type: str, target_id: str | Any, payload: dict | None = None) -> None:
    conn.execute(
        audit_log.insert().values(
            actor_email=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            payload=payload,
        )
    )


# ============================================================
# ADMIN
# ============================================================


def admin_list_products(conn: Connection) -> list[dict]:
    rows = conn.execute(select(products).order_by(products.c.product_id)).mappings().all()
    return [dict(r) for r in rows]


def _norm_base_url(raw: str | None) -> str | None:
    """Tenant API origin: scheme + host, no trailing slash, no /api/v1."""
    if not raw:
        return None
    u = str(raw).strip()
    if not u:
        return None
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/").removesuffix("/api/v1").rstrip("/")


def _tenant_row(conn: Connection, tenant_id: int) -> dict:
    row = (
        conn.execute(select(tenants).where(tenants.c.tenant_id == tenant_id)).mappings().first()
    )
    if row is None:
        raise NotFoundError(message=f"Tenant {tenant_id} not found")
    return dict(row)


def _subscription_row(conn: Connection, tenant_id: int) -> dict:
    row = (
        conn.execute(
            select(subscriptions).where(subscriptions.c.tenant_id == tenant_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError(message=f"Tenant {tenant_id} has no subscription")
    return dict(row)


def admin_list_tenants(conn: Connection, product_code: str | None = None) -> list[dict]:
    stmt = (
        select(
            tenants,
            products.c.code.label("product_code"),
            subscriptions.c.seat_limit,
            subscriptions.c.valid_until,
            subscriptions.c.status.label("subscription_status"),
            subscriptions.c.is_paid,
        )
        .select_from(
            tenants.join(products, products.c.product_id == tenants.c.product_id).join(
                subscriptions, subscriptions.c.tenant_id == tenants.c.tenant_id, isouter=True
            )
        )
        .order_by(tenants.c.tenant_id.desc())
    )
    if product_code:
        stmt = stmt.where(products.c.code == product_code)
    rows = conn.execute(stmt).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["seats_used"] = conn.execute(
            select(func.count())
            .select_from(device_activations)
            .where(
                device_activations.c.tenant_id == r["tenant_id"],
                device_activations.c.is_revoked.is_(False),
            )
        ).scalar_one()
        out.append(d)
    return out


def admin_create_tenant(conn: Connection, actor: str, body: dict) -> dict:
    prod = (
        conn.execute(
            select(products).where(products.c.code == str(body["product_code"]).lower())
        )
        .mappings()
        .first()
    )
    if prod is None:
        raise NotFoundError(message=f"Unknown product '{body['product_code']}'")
    slug = str(body["slug"]).strip().lower()
    if not slug:
        raise ValidationError(message="slug is required", field="slug")
    dup = conn.execute(
        select(tenants.c.tenant_id).where(
            tenants.c.product_id == prod["product_id"], tenants.c.slug == slug
        )
    ).first()
    if dup is not None:
        raise ConflictError(message=f"Tenant '{slug}' already exists for {prod['code']}")

    base_url = _norm_base_url(body.get("base_url"))
    if not base_url:
        raise ValidationError(message="base_url is required", field="base_url")

    tenant_id = conn.execute(
        tenants.insert()
        .values(
            product_id=prod["product_id"],
            slug=slug,
            name=str(body["name"]).strip(),
            contact_email=(body.get("contact_email") or None),
            base_url=base_url,
        )
        .returning(tenants.c.tenant_id)
    ).scalar_one()

    window_days = int(body.get("window_days") or settings.LM_DEFAULT_WINDOW_DAYS)
    conn.execute(
        subscriptions.insert().values(
            tenant_id=tenant_id,
            plan_name=body.get("plan_name") or "standard",
            seat_limit=int(body.get("seat_limit") or settings.LM_DEFAULT_SEAT_LIMIT),
            window_days=window_days,
            grace_days=int(body.get("grace_days") or settings.LM_DEFAULT_GRACE_DAYS),
            valid_until=_now() + timedelta(days=window_days),
        )
    )
    _audit(conn, actor, "tenant.create", "tenant", tenant_id, {"slug": slug, "product": prod["code"]})
    return admin_tenant_detail(conn, tenant_id)


def admin_tenant_detail(conn: Connection, tenant_id: int) -> dict:
    t = _tenant_row(conn, tenant_id)
    prod = conn.execute(
        select(products).where(products.c.product_id == t["product_id"])
    ).mappings().first()
    sub = _subscription_row(conn, tenant_id)
    toks = conn.execute(
        select(batch_tokens)
        .where(batch_tokens.c.subscription_id == sub["subscription_id"])
        .order_by(batch_tokens.c.batch_token_id.desc())
    ).mappings().all()
    devs = conn.execute(
        select(device_activations)
        .where(device_activations.c.tenant_id == tenant_id)
        .order_by(device_activations.c.device_activation_id.desc())
    ).mappings().all()
    pays = conn.execute(
        select(payments)
        .where(payments.c.subscription_id == sub["subscription_id"])
        .order_by(payments.c.recorded_at.desc())
        .limit(50)
    ).mappings().all()
    seats_used = sum(1 for d in devs if not d["is_revoked"])
    now = _now()
    return {
        "tenant": {**t, "product_code": prod["code"] if prod else None},
        "subscription": {
            **sub,
            "seats_used": seats_used,
            "seats_available": max(sub["seat_limit"] - seats_used, 0),
        },
        "batch_tokens": [dict(r) for r in toks],
        "devices": [
            {**dict(d), "state": _device_state(d, sub, now), "days_remaining": _days_remaining(d["valid_until"], now)}
            for d in devs
        ],
        "payments": [dict(r) for r in pays],
    }


def admin_patch_tenant(conn: Connection, actor: str, tenant_id: int, body: dict) -> dict:
    _tenant_row(conn, tenant_id)  # 404 if missing
    values: dict = {}
    if "name" in body and body["name"]:
        values["name"] = str(body["name"]).strip()
    if "contact_email" in body:
        values["contact_email"] = (body.get("contact_email") or None)
    if "base_url" in body:
        base_url = _norm_base_url(body.get("base_url"))
        if not base_url:
            raise ValidationError(message="base_url cannot be empty", field="base_url")
        values["base_url"] = base_url
    if values:
        conn.execute(tenants.update().where(tenants.c.tenant_id == tenant_id).values(**values))
        _audit(conn, actor, "tenant.patch", "tenant", tenant_id, {k: str(v) for k, v in values.items()})
    return admin_tenant_detail(conn, tenant_id)


def admin_patch_subscription(conn: Connection, actor: str, tenant_id: int, body: dict) -> dict:
    sub = _subscription_row(conn, tenant_id)
    values: dict[str, Any] = {"updated_at": _now()}
    for k in ("seat_limit", "window_days", "grace_days"):
        if body.get(k) is not None:
            values[k] = int(body[k])
    if body.get("is_paid") is not None:
        values["is_paid"] = bool(body["is_paid"])
    if body.get("status") in ("ACTIVE", "PAST_DUE", "SUSPENDED"):
        values["status"] = body["status"]
    if body.get("plan_name"):
        values["plan_name"] = str(body["plan_name"])
    conn.execute(
        subscriptions.update()
        .where(subscriptions.c.subscription_id == sub["subscription_id"])
        .values(**values)
    )
    _audit(conn, actor, "subscription.patch", "subscription", sub["subscription_id"], values)
    return admin_tenant_detail(conn, tenant_id)


def admin_generate_batch_token(conn: Connection, actor: str, tenant_id: int, body: dict) -> dict:
    t = _tenant_row(conn, tenant_id)
    prod = conn.execute(select(products).where(products.c.product_id == t["product_id"])).mappings().first()
    sub = _subscription_row(conn, tenant_id)
    quota = int(body.get("quota") or 0)
    if quota <= 0:
        raise ValidationError(message="quota must be > 0", field="quota")
    token = f"BATCH-{prod['code'].upper()}-{t['slug'].upper()}-{secrets.token_hex(4).upper()}"
    expires_at = body.get("expires_at")
    row = conn.execute(
        batch_tokens.insert()
        .values(
            subscription_id=sub["subscription_id"],
            token=token,
            quota=quota,
            expires_at=expires_at,
            note=(body.get("note") or None),
        )
        .returning(batch_tokens)
    ).mappings().one()
    _audit(conn, actor, "batch_token.create", "batch_token", row["batch_token_id"], {"quota": quota})
    return dict(row)


def admin_revoke_batch_token(conn: Connection, actor: str, batch_token_id: int) -> dict:
    row = conn.execute(
        batch_tokens.update()
        .where(batch_tokens.c.batch_token_id == batch_token_id)
        .values(is_revoked=True)
        .returning(batch_tokens)
    ).mappings().first()
    if row is None:
        raise NotFoundError(message=f"Batch token {batch_token_id} not found")
    _audit(conn, actor, "batch_token.revoke", "batch_token", batch_token_id)
    return dict(row)


def admin_record_payment(conn: Connection, actor: str, tenant_id: int, body: dict) -> dict:
    sub = _subscription_row(conn, tenant_id)
    amount = Decimal(str(body.get("amount") or "0"))
    if amount < 0:
        raise ValidationError(message="amount must be >= 0", field="amount")
    conn.execute(
        payments.insert().values(
            subscription_id=sub["subscription_id"],
            amount=amount,
            currency=body.get("currency") or "BOB",
            period_start=body.get("period_start"),
            period_end=body.get("period_end"),
            actor_email=actor,
            note=(body.get("note") or None),
        )
    )
    base = max(_now(), sub["valid_until"] if isinstance(sub["valid_until"], datetime) else _now())
    new_valid = base + timedelta(days=int(sub["window_days"]))
    conn.execute(
        subscriptions.update()
        .where(subscriptions.c.subscription_id == sub["subscription_id"])
        .values(valid_until=new_valid, is_paid=True, status="ACTIVE", updated_at=_now())
    )
    _audit(conn, actor, "payment.record", "subscription", sub["subscription_id"], {"amount": str(amount)})
    return admin_tenant_detail(conn, tenant_id)


def admin_set_tenant_status(conn: Connection, actor: str, tenant_id: int, status: str) -> dict:
    if status not in ("ACTIVE", "SUSPENDED", "ARCHIVED"):
        raise ValidationError(message="bad status", field="status")
    conn.execute(tenants.update().where(tenants.c.tenant_id == tenant_id).values(status=status))
    if status == "SUSPENDED":
        conn.execute(
            subscriptions.update()
            .where(subscriptions.c.tenant_id == tenant_id)
            .values(status="SUSPENDED", is_paid=False, updated_at=_now())
        )
    elif status == "ACTIVE":
        conn.execute(
            subscriptions.update()
            .where(subscriptions.c.tenant_id == tenant_id)
            .values(status="ACTIVE", updated_at=_now())
        )
    _audit(conn, actor, "tenant.status", "tenant", tenant_id, {"status": status})
    return admin_tenant_detail(conn, tenant_id)


def admin_mint_service_token(conn: Connection, actor: str, tenant_id: int, name: str) -> dict:
    t = _tenant_row(conn, tenant_id)
    raw = "svc_" + secrets.token_urlsafe(32)
    conn.execute(
        service_tokens.insert().values(
            product_id=t["product_id"],
            tenant_id=tenant_id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name=name.strip() or "service token",
        )
    )
    _audit(conn, actor, "service_token.mint", "tenant", tenant_id, {"name": name})
    # The raw token is returned exactly once.
    return {"token": raw}


def admin_revoke_device(conn: Connection, actor: str, hardware_uuid: str) -> dict:
    row = conn.execute(
        device_activations.update()
        .where(device_activations.c.hardware_uuid == hardware_uuid)
        .values(is_revoked=True)
        .returning(device_activations)
    ).mappings().first()
    if row is None:
        raise NotFoundError(message="No device bound to that hardware_uuid")
    _release_seat(conn, hardware_uuid)
    _audit(conn, actor, "device.revoke", "device", hardware_uuid)
    return dict(row)


def admin_delete_tenant(conn: Connection, actor: str, tenant_id: int) -> dict:
    """Hard delete a tenant. Cascades via FK ondelete=CASCADE."""
    _tenant_row(conn, tenant_id)  # 404 if missing
    conn.execute(tenants.delete().where(tenants.c.tenant_id == tenant_id))
    _audit(conn, actor, "tenant.delete", "tenant", tenant_id)
    return {"deleted": True, "tenant_id": tenant_id}


def admin_list_audit(conn: Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        select(audit_log).order_by(audit_log.c.at.desc()).limit(min(limit, 500))
    ).mappings().all()
    return [dict(r) for r in rows]


# ============================================================
# CONTROL PLANE (product backends)
# ============================================================


def _days_remaining(valid_until: datetime | None, now: datetime) -> int:
    if valid_until is None:
        return 0
    delta = (valid_until - now).total_seconds() / 86_400
    return math.floor(delta)


def _device_state(dev: dict, sub: dict | None, now: datetime) -> str:
    if dev["is_revoked"]:
        return _STATE_REVOKED
    dr = _days_remaining(dev["valid_until"], now)
    grace = int(sub["grace_days"]) if sub else settings.LM_DEFAULT_GRACE_DAYS
    if dr >= 0:
        return _STATE_ACTIVE
    if dr >= -grace:
        return _STATE_GRACE
    return _STATE_EXPIRED


def _tenant_for_service(conn: Connection, tenant_id: int | None) -> dict:
    if tenant_id is None:
        raise ForbiddenError(message="This service token is not scoped to a tenant")
    return _tenant_row(conn, tenant_id)


def cp_subscription(conn: Connection, tenant_id: int | None) -> dict:
    t = _tenant_for_service(conn, tenant_id)
    sub = _subscription_row(conn, t["tenant_id"])
    return {
        "state": sub["status"],
        "is_paid": bool(sub["is_paid"]) and t["status"] == "ACTIVE",
        "window_days": int(sub["window_days"]),
        "grace_days": int(sub["grace_days"]),
        "seat_limit": int(sub["seat_limit"]),
        "valid_until": _iso(sub["valid_until"]),
    }


def cp_seat_limit(conn: Connection, tenant_id: int | None) -> int:
    return int(_subscription_row(conn, _tenant_for_service(conn, tenant_id)["tenant_id"])["seat_limit"])


def _batch_token_row(conn: Connection, token: str) -> dict | None:
    return (
        conn.execute(select(batch_tokens).where(batch_tokens.c.token == token))
        .mappings()
        .first()
    )


def cp_verify_token(conn: Connection, tenant_id: int | None, token: str) -> dict:
    row = _batch_token_row(conn, token)
    now = _now()
    valid = bool(
        row
        and not row["is_revoked"]
        and row["seats_consumed"] < row["quota"]
        and (row["expires_at"] is None or row["expires_at"] > now)
    )
    if valid and tenant_id is not None:
        sub = _subscription_row(conn, tenant_id)
        valid = row["subscription_id"] == sub["subscription_id"]
    return {"valid": valid, "subscription_id": row["subscription_id"] if row else None}


def _consume_seat(conn: Connection, token_row: dict, hardware_uuid: str) -> None:
    updated = conn.execute(
        batch_tokens.update()
        .where(
            batch_tokens.c.batch_token_id == token_row["batch_token_id"],
            batch_tokens.c.seats_consumed < batch_tokens.c.quota,
        )
        .values(seats_consumed=batch_tokens.c.seats_consumed + 1)
    ).rowcount
    if not updated:
        raise SeatLimitReachedError(
            context={"quota": token_row["quota"], "seats_consumed": token_row["seats_consumed"]}
        )


def _release_seat(conn: Connection, hardware_uuid: str) -> None:
    row = (
        conn.execute(
            select(
                device_activations.c.activated_via,
                device_activations.c.is_revoked,
            ).where(device_activations.c.hardware_uuid == hardware_uuid)
        )
        .mappings()
        .first()
    )
    if not row or row["is_revoked"]:
        return
    if row["activated_via"]:
        conn.execute(
            batch_tokens.update()
            .where(
                batch_tokens.c.batch_token_id == row["activated_via"],
                batch_tokens.c.seats_consumed > 0,
            )
            .values(seats_consumed=batch_tokens.c.seats_consumed - 1)
        )
    # Free the seat for the tenant-wide active count too, so the portal and any
    # future re-activation see it as available.
    conn.execute(
        device_activations.update()
        .where(device_activations.c.hardware_uuid == hardware_uuid)
        .values(is_revoked=True)
    )


def cp_release_seat(conn: Connection, tenant_id: int | None, hardware_uuid: str) -> dict:
    _release_seat(conn, hardware_uuid)
    return {"released": True}


def cp_consume_seat(conn: Connection, tenant_id: int | None, body: dict) -> dict:
    """Book one seat against a batch token's quota for a device that the product
    backend manages itself (it mints and custodies its own license key in the
    tenant DB). This is the seat-accounting half of ``cp_activate`` without
    issuing a ``device_config.dat``. Idempotent for a hardware_uuid that already
    holds a live seat.
    """
    t = _tenant_for_service(conn, tenant_id)
    sub = _subscription_row(conn, t["tenant_id"])
    token = str(body["token"])
    tok = _batch_token_row(conn, token)
    now = _now()
    if (
        tok is None
        or tok["is_revoked"]
        or tok["subscription_id"] != sub["subscription_id"]
        or (tok["expires_at"] is not None and tok["expires_at"] <= now)
    ):
        raise ActivationTokenInvalidError()

    hw = str(body["hardware_uuid"])
    existing = (
        conn.execute(select(device_activations).where(device_activations.c.hardware_uuid == hw))
        .mappings()
        .first()
    )
    if existing is not None and not existing["is_revoked"]:
        conn.execute(
            device_activations.update()
            .where(device_activations.c.hardware_uuid == hw)
            .values(last_seen_at=now)
        )
        return {"consumed": False, "reactivated": True, "hardware_uuid": hw}

    active_seats = conn.execute(
        select(func.count())
        .select_from(device_activations)
        .where(
            device_activations.c.tenant_id == t["tenant_id"],
            device_activations.c.is_revoked.is_(False),
        )
    ).scalar_one()
    if active_seats >= int(sub["seat_limit"]):
        raise SeatLimitReachedError(
            context={"active_seats": active_seats, "seat_limit": sub["seat_limit"]}
        )

    _consume_seat(conn, dict(tok), hw)

    row_values = {
        "tenant_id": t["tenant_id"],
        "device_name": (str(body.get("device_name") or hw))[:100],
        "branch_ref": (body.get("branch_ref") or None),
        "license_key": "EXT-" + hw,  # real key is custodied in the product tenant DB
        "valid_from": now,
        "valid_until": now + timedelta(days=int(sub["window_days"])),
        "activated_via": tok["batch_token_id"],
        "is_revoked": False,
        "last_seen_at": now,
    }
    if existing is not None:  # previously revoked — re-book the same row
        conn.execute(
            device_activations.update()
            .where(device_activations.c.hardware_uuid == hw)
            .values(**row_values)
        )
    else:
        conn.execute(device_activations.insert().values(hardware_uuid=hw, **row_values))

    _audit(conn, f"cp:{t['slug']}", "cp_consume_seat", "device_activation", hw, {"token": token})
    return {"consumed": True, "reactivated": False, "hardware_uuid": hw}


def _device_config(payload: dict) -> str:
    # BUG FIX: sign_payload() signs the CANONICAL form of `payload`
    # (json.dumps(..., sort_keys=True, separators=(",", ":")) — see
    # signer.py's _canonical()). This function used to re-serialize the same
    # `payload` dict WITHOUT sort_keys=True when embedding it in the outer
    # {"payload": ..., "signature": ...} envelope — insertion order, not
    # sorted. A client extracting the raw "payload" substring from this
    # embedded JSON (the only correct way to verify byte-for-byte, since
    # JSON.parse -> JSON.stringify round-tripping isn't guaranteed to
    # reproduce the exact signed bytes) was therefore verifying different
    # bytes than what was actually signed — Ed25519 verification failed for
    # every device activation using real signing, unconditionally. Caught via
    # a real device activation client-side (SistemaVentas), confirmed here by
    # reproducing the byte mismatch directly. sort_keys=True on this call
    # makes the embedded payload substring byte-identical to what
    # sign_payload() actually signed.
    return json.dumps(
        {"payload": payload, "signature": sign_payload(payload)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _do_activate(conn: Connection, t: dict, sub: dict, tok: dict, body: dict, now: datetime) -> dict:
    """Bind hardware_uuid -> tenant, consume a seat, mint the key + signed config.
    Shared by ``cp_activate`` (service-token) and ``public_activate`` (batch token)."""
    hw = str(body["hardware_uuid"])
    existing = (
        conn.execute(select(device_activations).where(device_activations.c.hardware_uuid == hw))
        .mappings()
        .first()
    )
    if existing is not None and not existing["is_revoked"]:
        # Idempotent re-activation — refresh, no new seat.
        row = conn.execute(
            device_activations.update()
            .where(device_activations.c.hardware_uuid == hw)
            .values(valid_until=now + timedelta(days=int(sub["window_days"])), last_seen_at=now)
            .returning(device_activations)
        ).mappings().one()
        return _activation_result(dict(row), t, now, reactivated=True)

    active_seats = conn.execute(
        select(func.count())
        .select_from(device_activations)
        .where(
            device_activations.c.tenant_id == t["tenant_id"],
            device_activations.c.is_revoked.is_(False),
        )
    ).scalar_one()
    if active_seats >= int(sub["seat_limit"]):
        raise SeatLimitReachedError(
            context={"active_seats": active_seats, "seat_limit": sub["seat_limit"]}
        )
    _consume_seat(conn, dict(tok), hw)

    license_key = "LIC-" + secrets.token_urlsafe(32)
    row = conn.execute(
        device_activations.insert()
        .values(
            tenant_id=t["tenant_id"],
            hardware_uuid=hw,
            device_name=str(body["device_name"]).strip(),
            branch_ref=(body.get("branch_ref") or None),
            license_key=license_key,
            valid_from=now,
            valid_until=now + timedelta(days=int(sub["window_days"])),
            activated_via=tok["batch_token_id"],
        )
        .returning(device_activations)
    ).mappings().one()
    return _activation_result(dict(row), t, now)


def _token_ok(tok: dict | None, subscription_id: int, now: datetime) -> bool:
    return bool(
        tok
        and not tok["is_revoked"]
        and tok["subscription_id"] == subscription_id
        and (tok["expires_at"] is None or tok["expires_at"] > now)
    )


def cp_activate(conn: Connection, tenant_id: int | None, body: dict) -> dict:
    t = _tenant_for_service(conn, tenant_id)
    sub = _subscription_row(conn, t["tenant_id"])
    now = _now()
    tok = _batch_token_row(conn, str(body["token"]))
    if not _token_ok(tok, sub["subscription_id"], now):
        raise ActivationTokenInvalidError()
    return _do_activate(conn, t, sub, dict(tok), body, now)


def public_activate(conn: Connection, body: dict) -> dict:
    """First-run terminal activation (ADR-0015). Unauthenticated — the batch
    token itself scopes the tenant. Returns the tenant's API URL so the client
    never types (or guesses) a server address."""
    now = _now()
    tok = _batch_token_row(conn, str(body["token"]))
    if tok is None or tok["is_revoked"] or (tok["expires_at"] is not None and tok["expires_at"] <= now):
        raise ActivationTokenInvalidError()
    sub = (
        conn.execute(
            select(subscriptions).where(subscriptions.c.subscription_id == tok["subscription_id"])
        )
        .mappings()
        .first()
    )
    if sub is None:
        raise ActivationTokenInvalidError()
    t = _tenant_row(conn, sub["tenant_id"])
    if t["status"] != "ACTIVE":
        raise ForbiddenError(message=f"Tenant is {t['status']}")
    return _do_activate(conn, t, dict(sub), dict(tok), body, now)


def _activation_result(dev: dict, tenant: dict, now: datetime, *, reactivated: bool = False) -> dict:
    payload = {
        "schema": "device_config.dat/v1",
        "product_code": None,
        "tenant_slug": tenant["slug"],
        "tenant_base_url": tenant.get("base_url"),
        "device_name": dev["device_name"],
        "hardware_uuid": dev["hardware_uuid"],
        "license_key": dev["license_key"],
        "valid_from": _iso(dev["valid_from"]),
        "valid_until": _iso(dev["valid_until"]),
        "issued_at": _iso(now),
    }
    return {
        "device": {**dev, "valid_from": _iso(dev["valid_from"]), "valid_until": _iso(dev["valid_until"])},
        "tenant_slug": tenant["slug"],
        "tenant_base_url": tenant.get("base_url"),
        "license_key": dev["license_key"],
        "device_config": _device_config(payload),
        "reactivated": reactivated,
    }


def cp_heartbeat(conn: Connection, tenant_id: int | None, body: dict) -> dict:
    t = _tenant_for_service(conn, tenant_id)
    sub = _subscription_row(conn, t["tenant_id"])
    hw = str(body["hardware_uuid"])
    dev = (
        conn.execute(select(device_activations).where(device_activations.c.hardware_uuid == hw))
        .mappings()
        .first()
    )
    if dev is None:
        raise NotFoundError(message="No device bound to that hardware_uuid")
    if dev["is_revoked"]:
        raise ForbiddenError(message="This device license has been revoked")
    if dev["license_key"] != str(body.get("license_key", "")):
        raise ForbiddenError(message="license_key mismatch")
    now = _now()
    paid = bool(sub["is_paid"]) and t["status"] == "ACTIVE"
    if paid:
        conn.execute(
            device_activations.update()
            .where(device_activations.c.hardware_uuid == hw)
            .values(valid_until=now + timedelta(days=int(sub["window_days"])), last_seen_at=now)
        )
    else:
        conn.execute(
            device_activations.update()
            .where(device_activations.c.hardware_uuid == hw)
            .values(last_seen_at=now)
        )
    return cp_status(conn, tenant_id, hw)


def cp_status(conn: Connection, tenant_id: int | None, hardware_uuid: str) -> dict:
    now = _now()
    dev = (
        conn.execute(
            select(device_activations).where(device_activations.c.hardware_uuid == hardware_uuid)
        )
        .mappings()
        .first()
    )
    if dev is None:
        return {
            "hardware_uuid": hardware_uuid,
            "state": _STATE_UNKNOWN,
            "days_remaining": 0,
            "valid_until": None,
            "last_heartbeat_at": None,
            "is_revoked": False,
        }
    sub = None
    try:
        sub = _subscription_row(conn, dev["tenant_id"])
    except NotFoundError:
        pass
    return {
        "hardware_uuid": hardware_uuid,
        "state": _device_state(dict(dev), sub, now),
        "days_remaining": _days_remaining(dev["valid_until"], now),
        "valid_until": _iso(dev["valid_until"]),
        "last_heartbeat_at": _iso(dev["last_seen_at"]),
        "is_revoked": bool(dev["is_revoked"]),
    }


def cp_verify_device(conn: Connection, tenant_id: int | None, body: dict) -> dict:
    """Gate for a product backend's login: is this (hardware_uuid, license_key)
    an activated, non-revoked device still inside its validity + grace window?
    (ADR-0015)."""
    t = _tenant_for_service(conn, tenant_id)
    hw = str(body["hardware_uuid"])
    key = str(body.get("license_key") or "")
    now = _now()
    dev = (
        conn.execute(
            select(device_activations).where(
                device_activations.c.hardware_uuid == hw,
                device_activations.c.tenant_id == t["tenant_id"],
            )
        )
        .mappings()
        .first()
    )
    if dev is None:
        return {"valid": False, "state": _STATE_UNKNOWN, "reason": "device not activated"}
    if not key or key != dev["license_key"]:
        return {"valid": False, "state": _device_state(dict(dev), None, now), "reason": "license key mismatch"}
    sub = None
    try:
        sub = _subscription_row(conn, dev["tenant_id"])
    except NotFoundError:
        pass
    state = _device_state(dict(dev), sub, now)
    valid = state in (_STATE_ACTIVE, _STATE_GRACE)
    return {
        "valid": valid,
        "state": state,
        "valid_until": _iso(dev["valid_until"]),
        "days_remaining": _days_remaining(dev["valid_until"], now),
        "reason": None if valid else f"device is {state}",
    }
