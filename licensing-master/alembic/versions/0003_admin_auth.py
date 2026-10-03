"""native admin authentication — admin_users + admin_sessions

Revision ID: 0003_admin_auth
Revises: 0002_tenant_base_url
Create Date: 2026-10-03

Replaces Cloudflare Access as the admin identity (password + mandatory TOTP,
server-side sessions). Only the sha256 of a session token is stored.
"""

from __future__ import annotations

from alembic import op

revision = "0003_admin_auth"
down_revision = "0002_tenant_base_url"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS licensing.admin_users (
    admin_user_id  BIGSERIAL PRIMARY KEY,
    email          VARCHAR(180) NOT NULL,
    password_hash  TEXT         NOT NULL,
    totp_secret    VARCHAR(64)  NOT NULL,
    totp_last_step BIGINT,
    is_active      BOOLEAN      NOT NULL DEFAULT true,
    created_at     TIMESTAMP    NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMP,
    CONSTRAINT uq_admin_users_email UNIQUE (email),
    CONSTRAINT ck_admin_users_email_lower CHECK (email = lower(email))
);

CREATE TABLE IF NOT EXISTS licensing.admin_sessions (
    admin_session_id BIGSERIAL PRIMARY KEY,
    admin_user_id    BIGINT      NOT NULL
        REFERENCES licensing.admin_users (admin_user_id) ON DELETE CASCADE,
    token_hash       VARCHAR(64) NOT NULL,
    created_at       TIMESTAMP   NOT NULL,
    last_seen_at     TIMESTAMP   NOT NULL,
    expires_at       TIMESTAMP   NOT NULL,
    revoked_at       TIMESTAMP,
    ip               VARCHAR(64),
    user_agent       VARCHAR(300),
    CONSTRAINT uq_admin_sessions_token_hash UNIQUE (token_hash)
);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_user ON licensing.admin_sessions (admin_user_id);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_expires ON licensing.admin_sessions (expires_at);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DROP TABLE IF EXISTS licensing.admin_sessions;"
        "DROP TABLE IF EXISTS licensing.admin_users;"
    )
