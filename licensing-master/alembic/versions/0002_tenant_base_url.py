"""tenants.base_url — tenant public API origin for central activation (ADR-0015)

Revision ID: 0002_tenant_base_url
Revises: 0001_baseline
Create Date: 2026-09-09
"""

from __future__ import annotations

from alembic import op

revision = "0002_tenant_base_url"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE licensing.tenants ADD COLUMN IF NOT EXISTS base_url VARCHAR(300)"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE licensing.tenants DROP COLUMN IF EXISTS base_url"
    )
