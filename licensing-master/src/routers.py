"""HTTP surface for licensing-master (ADR-0013)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query
from sqlalchemy.engine import Connection

from src import service
from src.auth import AdminIdentity, ServiceIdentity, admin_identity, service_identity
from src.config import settings
from src.db import UnitOfWork, get_read_connection, get_write_uow
from src.envelope import Envelope, ok
from src.errors import ForbiddenError

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "licensing-master"}


@router.post("/activate", response_model=Envelope, status_code=201, tags=["activation"])
def activate(
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    body: Annotated[dict, Body()],
) -> Envelope:
    """First-run terminal activation. Unauthenticated — batch-token-gated. MUST
    be reachable without Cloudflare Access (ADR-0015)."""
    return ok(service.public_activate(uow.connection, body))


# ---- admin (Cloudflare Access) -----------------------------------------

admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/me", response_model=Envelope)
def whoami(ident: Annotated[AdminIdentity, Depends(admin_identity)]) -> Envelope:
    return ok({"email": ident.email})


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
    uow: Annotated[UnitOfWork, Depends(get_write_uow)],
    ident: Annotated[AdminIdentity, Depends(admin_identity)],
    x_admin_delete_password: Annotated[str | None, Header(alias="X-Admin-Delete-Password")] = None,
    body: Annotated[dict, Body()] = None,
) -> Envelope:
    """Hard delete a tenant (cascades via FK ondelete=CASCADE).
    Requires X-Admin-Delete-Password header or admin_password body field.
    """
    provided = x_admin_delete_password or (body or {}).get("admin_password")
    if provided != settings.LM_ADMIN_DELETE_PASSWORD:
        raise ForbiddenError(message="Invalid admin password")
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
