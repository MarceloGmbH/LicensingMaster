# Multi-stage build for licensing-master (ADR-0013).
# Build context is the repository root so both the API and the portal source are
# visible:
#   docker build -f licensing-master/Dockerfile -t licensing-master .
# (docker-compose.yml sets `context: .` for you.)

# --- stage 1: build the portal (apps/licensing-portal -> dist) -------------
FROM node:20-slim AS portal
WORKDIR /portal
COPY licensing-portal/package.json ./
RUN npm install --no-audit --no-fund
COPY licensing-portal/ ./
RUN npm run build

# --- stage 2: the API image ----------------------------------------------
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY licensing-master/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY licensing-master/ .
COPY --from=portal /portal/dist ./portal

RUN chmod +x ./entrypoint.sh

EXPOSE 8000

# entrypoint runs `alembic upgrade head` then launches uvicorn.
CMD ["./entrypoint.sh"]
