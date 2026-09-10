#!/bin/sh
# Apply pending migrations, then serve. Safe to re-run: `alembic upgrade head`
# is a no-op once the control-plane schema is current.
set -e

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

echo "[entrypoint] starting uvicorn on :8000"
exec uvicorn src.main:app \
    --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips='*'
