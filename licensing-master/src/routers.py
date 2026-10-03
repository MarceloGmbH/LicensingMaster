"""HTTP surface for licensing-master (ADR-0013)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.engine import Connection

from src import admin_auth, ratelimit, service
from src.auth import AdminIdentity, ServiceIdentity, admin_identity, csrf_guard, service_identity
from src.config import settings
from src.db import UnitOfWork, get_read_connection, get_write_uow
from src.envelope import Envelope, fail, ok
from src.errors import (
    ActivationTokenInvalidError,
    ForbiddenError,
    TooManyAttemptsError,
    UnauthorizedError,
)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "licensing-master"}


def _activation_gate(request: Request) -> str:
    """Reject locked-out client IPs before any DB work. Returns the client IP."""
    ip = ratelimit.client_ip(request)
    wait = ratelimit.activation_limiter.retry_after(ip)
    if wait:
        raise TooManyAttemptsError(headers={"Retry-After": str(wait)})
    return ip


@router.post("/activate", response_model=Envelope, status_code=201, tags=["activation"])
def activate(
    ip: Annotated[str, Depends(_activation_gate)],
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    body: Annotated[dict, Body()],
) -> Envelope:
    """First-run terminal activation. Unauthenticated — batch-token-gated. MUST
    stay reachable without an admin session (ADR-0015). Failed token attempts
    are throttled per client IP (429 TOO_MANY_ATTEMPTS)."""
    try:
        return ok(service.public_activate(uow.connection, body))
    except ActivationTokenInvalidError:
        ratelimit.activation_limiter.record_failure(ip)
        raise


# ---- admin (native login: password + TOTP, lm_session cookie) ------------

# Every state-changing /admin/* request (login included) must carry the portal's
# X-Requested-With header, on top of the SameSite=Strict session cookie.
admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(csrf_guard)])
auth_router = APIRouter(prefix="/auth", tags=["admin-auth"])


class LoginBody(BaseModel):
    email: str
    password: str
    totp_code: str


def _login_gate(email: str, ip: str) -> None:
    """429 (with Retry-After) while the e-mail or the client IP is locked out."""
    wait = max(
        ratelimit.login_email_limiter.retry_after(email),
        ratelimit.login_ip_limiter.retry_after(ip),
    )
    if wait:
        raise TooManyAttemptsError(headers={"Retry-After": str(wait)})


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _session_cookie_kwargs() -> dict:
    return {"httponly": True, "secure": not settings.is_dev, "samesite": "strict", "path": "/"}


@auth_router.post("/login", response_model=Envelope)
def login(
    request: Request,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    body: LoginBody,
):
    ip = ratelimit.client_ip(request)
    email = admin_auth.normalize_email(body.email)[:180]
    _login_gate(email, ip)
    result = admin_auth.login(
        uow.connection,
        email,
        body.password,
        body.totp_code,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    if result is None:
        ratelimit.login_email_limiter.record_failure(email)
        ratelimit.login_ip_limiter.record_failure(ip)
        # Returned (not raised) so the admin.login_failed audit row is committed.
        err = UnauthorizedError(message="Invalid credentials")
        return _no_store(JSONResponse(status_code=401, content=fail(err.to_error_detail()).model_dump(mode="json")))
    raw, email = result
    ratelimit.login_email_limiter.clear(email)
    resp = _no_store(JSONResponse(content=ok({"email": email}).model_dump(mode="json")))
    resp.set_cookie(
        admin_auth.SESSION_COOKIE,
        raw,
        max_age=settings.LM_ADMIN_SESSION_ABSOLUTE_SECONDS,
        **_session_cookie_kwargs(),
    )
    return resp


@auth_router.post("/logout", response_model=Envelope)
def logout(request: Request, uow: Annotated[UnitOfWork, Depends(get_write_uow)]):
    raw = request.cookies.get(admin_auth.SESSION_COOKIE)
    email = admin_auth.revoke_session(uow.connection, raw) if raw else None
    if email:
        service._audit(uow.connection, email, "admin.logout", "admin_user", email)
    resp = _no_store(JSONResponse(content=ok({"logged_out": True}).model_dump(mode="json")))
    resp.delete_cookie(admin_auth.SESSION_COOKIE, **_session_cookie_kwargs())
    return resp


@auth_router.get("/me", response_model=Envelope)
def whoami(ident: Annotated[AdminIdentity, Depends(admin_identity)]):
    return _no_store(JSONResponse(content=ok({"email": ident.email}).model_dump(mode="json")))


admin.include_router(auth_router)


@admin.get("/products", response_model=Envelope)
def products(conn: Annotated[Connection, Depends(get_read_connection)], _: Annotated[AdminIdentity, Depends(admin_identity)]) -> Envelope:
    return ok({"items": service.admin_list_products(conn)})


@admin.get("/tenants", response_model=Envelope)
def tenants(
    conn: Annotated[Connection, Depends(get_read_connection)],
    _: Annotated[AdminIdentity, Depends(admin_identity)],
    product: Annotated[str | None, Query()] = None,
) -> Envelope:
    return ok({"items": service.admin_list_tenants(conn, product)})


@admin.post("/tenants", response_model=Envelope, status_code=201)
def create_tenant(
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.admin_create_tenant(uow.connection, ident.email, body))


@admin.get("/tenants/{tenant_id}", response_model=Envelope)
def tenant_detail(
    tenant_id: int,
    conn: Annotated[Connection, Depends(get_read_connection)],
    _: Annotated[AdminIdentity, Depends(admin_identity)],
) -> Envelope:
    return ok(service.admin_tenant_detail(conn, tenant_id))


@admin.patch("/tenants/{tenant_id}", response_model=Envelope)
def patch_tenant(
    tenant_id: int,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.admin_patch_tenant(uow.connection, ident.email, tenant_id, body))


@admin.delete("/tenants/{tenant_id}", response_model=Envelope)
def delete_tenant(
    tenant_id: int,
    request: Request,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict | None, Body()] = None,
) -> Envelope:
    """Hard delete a tenant (cascades via FK ondelete=CASCADE). Requires the
    logged-in admin's OWN password in the body (`admin_password`); wrong guesses
    count towards the login lockout of that admin and of the client IP."""
    ip = ratelimit.client_ip(request)
    _login_gate(ident.email, ip)
    provided = (body or {}).get("admin_password")
    if not isinstance(provided, str) or not admin_auth.verify_admin_password(
        uow.connection, ident.email, provided
    ):
        ratelimit.login_email_limiter.record_failure(ident.email)
        ratelimit.login_ip_limiter.record_failure(ip)
        raise ForbiddenError(message="Invalid password")
    return ok(service.admin_delete_tenant(uow.connection, ident.email, tenant_id))


@admin.patch("/tenants/{tenant_id}/subscription", response_model=Envelope)
def patch_subscription(
    tenant_id: int,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.admin_patch_subscription(uow.connection, ident.email, tenant_id, body))


@admin.post("/tenants/{tenant_id}/batch-tokens", response_model=Envelope, status_code=201)
def gen_batch_token(
    tenant_id: int,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.admin_generate_batch_token(uow.connection, ident.email, tenant_id, body))


@admin.post("/batch-tokens/{batch_token_id}/revoke", response_model=Envelope)
def revoke_batch_token(
    batch_token_id: int,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
) -> Envelope:
    return ok(service.admin_revoke_batch_token(uow.connection, ident.email, batch_token_id))


@admin.post("/tenants/{tenant_id}/payments", response_model=Envelope, status_code=201)
def record_payment(
    tenant_id: int,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.admin_record_payment(uow.connection, ident.email, tenant_id, body))


@admin.post("/tenants/{tenant_id}/suspend", response_model=Envelope)
def suspend(tenant_id: int, uow: Annotated[UnitOfWork, Depends(get_write_uow)], ident: Annotated[AdminIdentity, Depends(admin_identity)]) -> Envelope:
    return ok(service.admin_set_tenant_status(uow.connection, ident.email, tenant_id, "SUSPENDED"))


@admin.post("/tenants/{tenant_id}/reactivate", response_model=Envelope)
def reactivate(tenant_id: int, uow: Annotated[UnitOfWork, Depends(get_write_uow)], ident: Annotated[AdminIdentity, Depends(admin_identity)]) -> Envelope:
    return ok(service.admin_set_tenant_status(uow.connection, ident.email, tenant_id, "ACTIVE"))


@admin.post("/tenants/{tenant_id}/service-tokens", response_model=Envelope, status_code=201)
def mint_service_token(
    tenant_id: int,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.admin_mint_service_token(uow.connection, ident.email, tenant_id, str(body.get("name", ""))))


@admin.post("/devices/{hardware_uuid}/revoke", response_model=Envelope)
def revoke_device(
    hardware_uuid: str,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
) -> Envelope:
    return ok(service.admin_revoke_device(uow.connection, ident.email, hardware_uuid))


@admin.post("/devices/{hardware_uuid}/reinstate", response_model=Envelope)
def reinstate_device(
    hardware_uuid: str,
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
) -> Envelope:
    return ok(service.admin_reinstate_device(uow.connection, ident.email, hardware_uuid))


@admin.get("/audit", response_model=Envelope)
def audit(
    conn: Annotated[Connection, Depends(get_read_connection)],
    _: Annotated[AdminIdentity, Depends(admin_identity)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Envelope:
    return ok({"items": service.admin_list_audit(conn, limit)})


# ---- control plane (service token) -----------------------------------

cp = APIRouter(prefix="/cp", tags=["control-plane"])


@cp.get("/subscription", response_model=Envelope)
def cp_subscription(conn: Annotated[Connection, Depends(get_read_connection)], ident: Annotated[ServiceIdentity, Depends(service_identity)]) -> Envelope:
    return ok(service.cp_subscription(conn, ident.tenant_id))


@cp.get("/seat-limit", response_model=Envelope)
def cp_seat_limit(conn: Annotated[Connection, Depends(get_read_connection)], ident: Annotated[ServiceIdentity, Depends(service_identity)]) -> Envelope:
    return ok({"seat_limit": service.cp_seat_limit(conn, ident.tenant_id)})


@cp.post("/activation-tokens/verify", response_model=Envelope)
def cp_verify(
    conn: Annotated[Connection, Depends(get_read_connection)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.cp_verify_token(conn, ident.tenant_id, str(body["token"])))


@cp.post("/devices/activate", response_model=Envelope, status_code=201)
def cp_activate(
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.cp_activate(uow.connection, ident.tenant_id, body))


@cp.post("/devices/heartbeat", response_model=Envelope)
def cp_heartbeat(
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.cp_heartbeat(uow.connection, ident.tenant_id, body))


@cp.get("/devices/status", response_model=Envelope)
def cp_status(
    conn: Annotated[Connection, Depends(get_read_connection)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    hardware_uuid: Annotated[str, Query(min_length=6)],
) -> Envelope:
    return ok(service.cp_status(conn, ident.tenant_id, hardware_uuid))


@cp.post("/devices/verify", response_model=Envelope)
def cp_verify_device(
    conn: Annotated[Connection, Depends(get_read_connection)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.cp_verify_device(conn, ident.tenant_id, body))


@cp.post("/seats/consume", response_model=Envelope, status_code=201)
def cp_consume(
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.cp_consume_seat(uow.connection, ident.tenant_id, body))


@cp.post("/seats/release", response_model=Envelope)
def cp_release(
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[ServiceIdentity, Depends(service_identity)],
    body: Annotated[dict, Body()],
) -> Envelope:
    return ok(service.cp_release_seat(uow.connection, ident.tenant_id, str(body["hardware_uuid"])))


router.include_router(admin)
router.include_router(cp)
