# Deploying `licensing-master`

The central multi-product licensing control plane (ADR-0013). One stack:
Postgres + the FastAPI app (`licensing-master`, portal bundled at `/`).

Ingress is the **shared Traefik on vm-gateway** — this repo does not carry a
reverse proxy. A dormant `cloudflared` service is defined (profile `tunnel`) for
a possible future switch, but it is OFF.

## Two hostnames, one container

| Hostname | Serves | Auth | Cloudflare |
|---|---|---|---|
| `licensing.alanadev.com` | portal + `/admin/*` | **Cloudflare Access** (operator only) | proxied (orange) |
| `licensing-cp.alanadev.com` | `/cp/*` + `/activate` + `/health` | **service token** / **batch token** — Access must NOT cover this | DNS-only (grey) |

`/cp/*` cannot sit behind Access: product backends are machines with no browser
session. They send a bearer service token that `licensing-master` hashes and
checks against `licensing.service_tokens`.

The app routes by path internally, so both hostnames target the same container.
Traefik's `licensing.yml` on vm-gateway has two path-scoped routers, both to
`http://10.10.4.10:3013`.

## 1. Bring up the stack (vm-services)

```bash
cp .env.example .env
# fill in: LM_POSTGRES_PASSWORD, LM_ADMIN_EMAILS, LM_ACCESS_TEAM_DOMAIN, LM_ACCESS_AUD
# keep:    LM_APP_ENV=production  LM_BIND_ADDR=0.0.0.0  LM_HOST_PORT=3013

# fresh environment only (no existing data volume):
docker volume create licensing_lm_pgdata

docker compose up -d --build
```

`entrypoint.sh` runs `alembic upgrade head` on start, seeding the schema and the
`farmacia` / `hotel` / `pos_pub` product rows. The container publishes
`0.0.0.0:3013 -> 8000`; nothing else reaches it.

## 2. Verify

```bash
curl -s http://127.0.0.1:3013/health                      # {"status":"ok",...}
curl -s https://licensing-cp.alanadev.com/health          # same, no login wall
curl -sI https://licensing.alanadev.com/ | grep -i location # 302 -> cloudflareaccess.com
curl -s https://licensing-cp.alanadev.com/cp/subscription  # 401 JSON, NOT an HTML login page
```

If `licensing-cp` returns HTML, an Access application is wrongly covering that
host — remove it.

## 3. Cloudflare Access application (portal only)

Zero Trust → **Access controls → Applications → Create new application →
Self-hosted**. Domain: subdomain `licensing`, domain `alanadev.com`, path blank
(covers the portal and `/admin/*`, not `licensing-cp`). Policy: Allow, Include →
Emails → the operator address. Copy the **Application Audience (AUD) tag** into
`.env` as `LM_ACCESS_AUD`; set `LM_ACCESS_TEAM_DOMAIN` to
`<team>.cloudflareaccess.com`. Then `docker compose up -d`.

> Do **not** create an Access application for `licensing-cp.alanadev.com`.

## 4. Wire a product backend

In the portal: open the tenant → **Emitir service token** → copy the `svc_...`
value once. On the product backend:

```
SF_LICENSING_BASE_URL=https://licensing-cp.alanadev.com
SF_LICENSING_SERVICE_TOKEN=svc_xxxxxxxx
```

Full contract: **[docs/INTEGRATION.md](docs/INTEGRATION.md)**.

## 5. Backups

Only `licensing-db` holds state:

```bash
docker compose exec licensing-db \
  pg_dump -U postgres -Fc sistema_farmacia_licensing > licensing-$(date +%F).dump
```

## 6. Switching to a real Cloudflare Tunnel (optional, not today)

Set `CF_TUNNEL_TOKEN` in `.env`, add the two public hostnames on the tunnel
pointing at `http://licensing-master:8000`, run
`docker compose --profile tunnel up -d`, then remove the licensing routers from
Traefik's `licensing.yml`. Trade-offs are in the Fase 1 design doc.
