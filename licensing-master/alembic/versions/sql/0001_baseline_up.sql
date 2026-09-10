-- =============================================================================
-- sistema_farmacia_licensing — central multi-product licensing control plane
-- (ADR-0013). One database, shared across every first-party product.
-- Applied ONCE (this is not a per-tenant database).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS licensing;

-- --------------------------------------------------------------------------
-- products — the first-party applications this plane licenses.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.products (
    product_id BIGSERIAL PRIMARY KEY,
    code VARCHAR(30) NOT NULL UNIQUE CHECK (code = lower(code)),
    name VARCHAR(120) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- tenants — one customer deployment of one product.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.tenants (
    tenant_id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES licensing.products(product_id),
    slug VARCHAR(60) NOT NULL,
    name VARCHAR(180) NOT NULL,
    contact_email VARCHAR(180),
    base_url VARCHAR(300),                        -- tenant's public API origin, injected into device_config on central activation
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    UNIQUE (product_id, slug)
);

-- --------------------------------------------------------------------------
-- subscriptions — the paid entitlement for one tenant (1:1). Seat quota +
-- validity window live here; a recorded payment pushes valid_until forward.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.subscriptions (
    subscription_id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL UNIQUE REFERENCES licensing.tenants(tenant_id) ON DELETE CASCADE,
    plan_name VARCHAR(80) NOT NULL DEFAULT 'standard',
    seat_limit INTEGER NOT NULL DEFAULT 1 CHECK (seat_limit >= 0),
    window_days INTEGER NOT NULL DEFAULT 30 CHECK (window_days > 0),
    grace_days INTEGER NOT NULL DEFAULT 5 CHECK (grace_days >= 0),
    valid_until TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'PAST_DUE', 'SUSPENDED')),
    is_paid BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- batch_tokens — quota-bound activation tokens the operator hands to a
-- customer/technician. seats_consumed is bumped on each activation.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.batch_tokens (
    batch_token_id BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT NOT NULL REFERENCES licensing.subscriptions(subscription_id) ON DELETE CASCADE,
    token VARCHAR(200) NOT NULL UNIQUE,
    quota INTEGER NOT NULL CHECK (quota > 0),
    seats_consumed INTEGER NOT NULL DEFAULT 0 CHECK (seats_consumed >= 0),
    expires_at TIMESTAMP WITHOUT TIME ZONE,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    note VARCHAR(300),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    CHECK (seats_consumed <= quota)
);

-- --------------------------------------------------------------------------
-- device_activations — the source-of-truth registry of every bound POS
-- terminal across every product. Each per-tenant sync.device_licenses row
-- is a downstream cache of one of these.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.device_activations (
    device_activation_id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES licensing.tenants(tenant_id) ON DELETE CASCADE,
    hardware_uuid VARCHAR(150) NOT NULL UNIQUE,
    device_name VARCHAR(100) NOT NULL,
    branch_ref VARCHAR(60),                       -- opaque branch id/code from the product backend
    license_key TEXT NOT NULL,                    -- minted here: LIC-<token_urlsafe(32)>
    valid_from TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    valid_until TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    last_seen_at TIMESTAMP WITHOUT TIME ZONE,
    activated_via BIGINT REFERENCES licensing.batch_tokens(batch_token_id),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_device_activations_tenant ON licensing.device_activations (tenant_id, is_revoked);

-- --------------------------------------------------------------------------
-- payments — an operator-recorded payment for a subscription. Recording one
-- extends subscriptions.valid_until by window_days from max(now, valid_until).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.payments (
    payment_id BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT NOT NULL REFERENCES licensing.subscriptions(subscription_id) ON DELETE CASCADE,
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    currency VARCHAR(10) NOT NULL DEFAULT 'BOB',
    period_start TIMESTAMP WITHOUT TIME ZONE,
    period_end TIMESTAMP WITHOUT TIME ZONE,
    recorded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    actor_email VARCHAR(180),
    note VARCHAR(300)
);
CREATE INDEX IF NOT EXISTS ix_payments_subscription ON licensing.payments (subscription_id, recorded_at DESC);

-- --------------------------------------------------------------------------
-- service_tokens — the credential a product backend presents on /cp/* calls.
-- Only the hash is stored. Scoped to a product, optionally to one tenant.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.service_tokens (
    service_token_id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES licensing.products(product_id),
    tenant_id BIGINT REFERENCES licensing.tenants(tenant_id) ON DELETE CASCADE,
    token_hash VARCHAR(128) NOT NULL UNIQUE,      -- sha256 hex of the raw token
    name VARCHAR(120) NOT NULL,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    last_used_at TIMESTAMP WITHOUT TIME ZONE
);

-- --------------------------------------------------------------------------
-- audit_log — append-only record of every admin mutation. actor_email is the
-- verified Cloudflare Access identity.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS licensing.audit_log (
    audit_log_id BIGSERIAL PRIMARY KEY,
    actor_email VARCHAR(180) NOT NULL,
    action VARCHAR(60) NOT NULL,
    target_type VARCHAR(40) NOT NULL,
    target_id VARCHAR(80),
    payload JSONB,
    at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_log_at ON licensing.audit_log (at DESC);

-- --------------------------------------------------------------------------
-- Seed the known first-party products (idempotent).
-- --------------------------------------------------------------------------
INSERT INTO licensing.products (code, name) VALUES
    ('farmacia', 'SistemaFarmacia V2'),
    ('hotel', 'Sistema Hotel'),
    ('pos_pub', 'POS PUB')
ON CONFLICT (code) DO NOTHING;
