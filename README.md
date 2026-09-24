# licensing-master

Central multi-product licensing control plane (ADR-0013). One FastAPI service +
one Postgres database (`sistema_farmacia_licensing`). Issues and accounts device
licenses / seats for every alanadev product (`farmacia`, `hotel`, `pos_pub`, …).

Split out of the SistemaFarmacia monorepo — this repo is autonomous: it builds,
tests and deploys without any part of `apps/backend`.

## Layout

```
licensing-master/     FastAPI app + Alembic migrations + smoke tests
licensing-portal/     React admin UI (built into the image, served at "/")
docker-compose.yml    the deployable stack (db + app + dormant cloudflared)
.env.example          configuration template
deploy.md             how it runs behind the shared Traefik on vm-gateway
docs/INTEGRATION.md   self-contained guide for integrating any other product
docs/FRONTEND-REQUIREMENTS.md   client-side blueprint: stack, installer, storage, retrofit checklist
```

## Run it

```bash
cp .env.example .env      # fill in the secrets
docker volume create licensing_lm_pgdata   # fresh env only
docker compose up -d --build
```

Migrations apply on container start. See `deploy.md`.

## Integrating a product

Read **[docs/INTEGRATION.md](docs/INTEGRATION.md)** — endpoints, auth, the
activation flow, failure handling and a reference client. You do not need to read
this service's source to integrate against it.

## Tests

```bash
cd licensing-master
LM_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/sistema_farmacia_licensing \
APP_ENV=test pytest tests -q
```
