"""SQLAlchemy Core table mirrors for `sistema_farmacia_licensing` (ADR-0013).

Kept byte-consistent with alembic/versions/sql/0001_baseline_up.sql.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.sql import func

metadata = MetaData(schema="licensing")

_TS = TIMESTAMP(timezone=False)

products = Table(
    "products",
    metadata,
    Column("product_id", BigInteger, primary_key=True),
    Column("code", String(30), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", BigInteger, primary_key=True),
    Column("product_id", BigInteger, ForeignKey("licensing.products.product_id"), nullable=False),
    Column("slug", String(60), nullable=False),
    Column("name", String(180), nullable=False),
    Column("contact_email", String(180)),
    Column("base_url", String(300)),
    Column("status", String(20), nullable=False, server_default="ACTIVE"),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("subscription_id", BigInteger, primary_key=True),
    Column(
        "tenant_id",
        BigInteger,
        ForeignKey("licensing.tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("plan_name", String(80), nullable=False, server_default="standard"),
    Column("seat_limit", Integer, nullable=False, server_default="1"),
    Column("window_days", Integer, nullable=False, server_default="30"),
    Column("grace_days", Integer, nullable=False, server_default="5"),
    Column("valid_until", _TS, nullable=False),
    Column("status", String(20), nullable=False, server_default="ACTIVE"),
    Column("is_paid", Boolean, nullable=False, server_default="true"),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
)

batch_tokens = Table(
    "batch_tokens",
    metadata,
    Column("batch_token_id", BigInteger, primary_key=True),
    Column(
        "subscription_id",
        BigInteger,
        ForeignKey("licensing.subscriptions.subscription_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("token", String(200), nullable=False, unique=True),
    Column("quota", Integer, nullable=False),
    Column("seats_consumed", Integer, nullable=False, server_default="0"),
    Column("expires_at", _TS),
    Column("is_revoked", Boolean, nullable=False, server_default="false"),
    Column("note", String(300)),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

device_activations = Table(
    "device_activations",
    metadata,
    Column("device_activation_id", BigInteger, primary_key=True),
    Column(
        "tenant_id",
        BigInteger,
        ForeignKey("licensing.tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("hardware_uuid", String(150), nullable=False, unique=True),
    Column("device_name", String(100), nullable=False),
    Column("branch_ref", String(60)),
    Column("license_key", Text, nullable=False),
    Column("valid_from", _TS, nullable=False, server_default=func.now()),
    Column("valid_until", _TS, nullable=False),
    Column("is_revoked", Boolean, nullable=False, server_default="false"),
    Column("last_seen_at", _TS),
    Column("activated_via", BigInteger, ForeignKey("licensing.batch_tokens.batch_token_id")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

payments = Table(
    "payments",
    metadata,
    Column("payment_id", BigInteger, primary_key=True),
    Column(
        "subscription_id",
        BigInteger,
        ForeignKey("licensing.subscriptions.subscription_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("amount", Numeric(14, 2), nullable=False),
    Column("currency", String(10), nullable=False, server_default="BOB"),
    Column("period_start", _TS),
    Column("period_end", _TS),
    Column("recorded_at", _TS, nullable=False, server_default=func.now()),
    Column("actor_email", String(180)),
    Column("note", String(300)),
)

service_tokens = Table(
    "service_tokens",
    metadata,
    Column("service_token_id", BigInteger, primary_key=True),
    Column("product_id", BigInteger, ForeignKey("licensing.products.product_id"), nullable=False),
    Column("tenant_id", BigInteger, ForeignKey("licensing.tenants.tenant_id", ondelete="CASCADE")),
    Column("token_hash", String(128), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("is_revoked", Boolean, nullable=False, server_default="false"),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("last_used_at", _TS),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("audit_log_id", BigInteger, primary_key=True),
    Column("actor_email", String(180), nullable=False),
    Column("action", String(60), nullable=False),
    Column("target_type", String(40), nullable=False),
    Column("target_id", String(80)),
    Column("payload", JSONB),
    Column("at", _TS, nullable=False, server_default=func.now()),
)
