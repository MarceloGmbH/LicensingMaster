"""baseline schema — sistema_farmacia_licensing

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-06

Raw DDL in ``sql/0001_baseline_up.sql`` (ADR-0013). Applied once — this is the
central control-plane database, not a per-tenant one.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).parent / "sql"
_UP_SQL = (_SQL_DIR / "0001_baseline_up.sql").read_text()


def upgrade() -> None:
    dbapi_conn = op.get_bind().connection.dbapi_connection
    with dbapi_conn.cursor() as cur:
        cur.execute(_UP_SQL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP SCHEMA IF EXISTS licensing CASCADE;")
