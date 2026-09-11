# Deploying `licensing-master` behind Cloudflare Tunnel + Access

The central licensing control plane (ADR-0013). One deployable: Postgres +
FastAPI (`licensing-master`, portal bundled at `/`) + a `cloudflared` connector.
No public IP, no published ports — the only ingress is the tunnel.

Two hostnames, one tunnel:

| Hostname | Serves | Auth |
|---|---|---|
| `licensing.alanadev.com` | the portal + `/admin/*` | **Cloudflare Access** (only you) |
| `licensing-cp.alanadev.com` | `/cp/*` only | **service token** (product backends) — Access must NOT cover this |

`/cp/*` cannot sit behind Access: product backends are machines, they have no
browser session. They authenticate with a bearer service token that
`licensing-master` hashes and checks against `licensing.service_tokens`.

---

## 1. Bring up the stack (on the box, no public IP needed)

```bash
cp .env.licensing.example .env.licensing
# fill in: LM_POSTGRES_PASSWORD, LM_ADMIN_EMAILS, LM_ACCESS_TEAM_DOMAIN,
#          LM_ACCESS_AUD (step 4), CF_TUNNEL_TOKEN (step 2)

docker compose --env-file .env.licensing -f docker-compose.licensing.yml up -d --build
```

`entrypoint.sh` runs `alembic upgrade head` on start, so the schema and the
`farmacia` / `hotel` / `pos_pub` product rows are seeded automatically. The
`licensing-master` container listens on `:8000` on the internal `lm` network
only — `cloudflared` is the sole thing that reaches it.

---

## 2. Create the tunnel (Cloudflare dashboard)

**Zero Trust → Networks → Tunnels → Create a tunnel**

1. Connector type: **Cloudflared**. Name it e.g. `licensing`.
2. On the "Install and run" screen, copy the token — the long string after
   `--token` in the shown `cloudflared ... run --token eyJ...` command.
3. Put it in `.env.licensing` as `CF_TUNNEL_TOKEN=...` and
   `docker compose ... up -d` again so the `cloudflared` service picks it up.
   The tunnel shows **HEALTHY** once the connector dials home.

You do **not** run the `cloudflared` install command from the dashboard by
hand — the compose service (`cloudflare/cloudflared`, `tunnel run`) is that
connector.

---

## 3. Public hostnames → the container (tunnel "Published application routes")

On the tunnel's **Public Hostname** tab, add two:

| Subdomain | Domain | Path | Service |
|---|---|---|---|
| `licensing` | `alanadev.com` | *(empty)* | `http://licensing-master:8000` |
| `licensing-cp` | `alanadev.com` | *(empty)* | `http://licensing-master:8000` |

`licensing-master` is the compose service name; `cloudflared` resolves it on
the shared `lm` network. Cloudflare creates the two proxied `CNAME`s in DNS for
you (both point at `<tunnel-id>.cfargotunnel.com`).

The app routes by path internally, so both hostnames can target the same
service — Access on the next step is what makes them behave differently.

---

## 4. Cloudflare Access application for the portal

> Dashboard note (2026): the old **Access → Applications → Add an application**
> path is now **Access controls → Applications → Create new application →
> Self-hosted and private**. A Cloudflare Tunnel is NOT required here — the
> hostname just has to be publicly routable (it is, via the proxied DNS record /
> your reverse proxy).

**Step 0 — onboard Zero Trust once.** If the left nav has no "Access controls",
open `one.dash.cloudflare.com`, pick a team name (this becomes
`<team>.cloudflareaccess.com`) and the free plan.

**Step 1 — a login method.** Zero Trust → **Settings → Authentication → Login
methods**. If you have no identity provider, add **One-time PIN** (email code,
zero setup).

**Step 2 — create the application.** Zero Trust → **Access controls →
Applications → Create new application → Self-hosted and private**:

- **Application name:** `Licensing portal`
- **Domain:** subdomain `licensing`, domain `alanadev.com`, path blank
  → covers the portal AND `/admin/*`, and does **not** touch
  `licensing-cp.alanadev.com`.
- **Session duration:** 24h (your call)
- **Access policies → create a policy:**
  - **Name:** `Only me`
  - **Action:** Allow
  - **Add a rule → Include →** selector **Emails** → `marcelogh.teamviewer@gmail.com`
    (more operators later; keep them in `LM_ADMIN_EMAILS` too — `licensing-master`
    re-checks the email after verifying the JWT, defence in depth).
- Create the application.

**Step 3 — copy the AUD.** Back on **Applications**, click **Configure** on
`Licensing portal` → **Overview** tab (or **Additional settings**) → copy the
**Application Audience (AUD) Tag** → `.env.licensing` as `LM_ACCESS_AUD=...`.

**Team domain:** Zero Trust → **Settings** (top of the page, "Team domain"), e.g.
`alanadev.cloudflareaccess.com` → `.env.licensing` as `LM_ACCESS_TEAM_DOMAIN=...`.

Then set `LM_APP_ENV=production` in `.env.licensing` and
`docker compose --env-file .env.licensing -f docker-compose.licensing.yml up -d`.
`licensing-master` fetches the JWKS from
`https://<team>/cdn-cgi/access/certs` to verify `Cf-Access-Jwt-Assertion`.

> Do **not** create an Access application for `licensing-cp.alanadev.com`.
> If you ever do, `/cp/*` calls from the product backends will get the Access
> login page instead of JSON.

### Verifying

- `https://licensing.alanadev.com` → Cloudflare login → the portal.
  `GET /admin/me` should echo your email.
- `curl https://licensing-cp.alanadev.com/health` → `{"status":"ok",...}` with
  no login wall.
- `curl https://licensing-cp.alanadev.com/cp/subscription` → `401` (needs the
  bearer token) — **not** an HTML login page. If you get HTML, an Access app is
  covering the `-cp` host; remove it.

---

## 5. Wire a product backend (pharmacy) to the control plane

In the portal: open the tenant → **Emitir service token** → copy the
`svc_...` value once (it is not shown again).

On the pharmacy backend (`apps/backend`), set:

```
SF_LICENSING_BASE_URL=https://licensing-cp.alanadev.com
SF_LICENSING_SERVICE_TOKEN=svc_xxxxxxxx
```

With both set, `licensing.provider` swaps `LicensingControlPlaneStub` for
`LicensingControlPlaneHTTP`, which calls:

| Port method | Endpoint |
|---|---|
| `verify_activation_token` | `POST /cp/activation-tokens/verify` |
| `check_subscription` | `GET /cp/subscription` |
| `seat_limit` | `GET /cp/seat-limit` |
| `consume_seat` | `POST /cp/seats/consume` |
| `release_seat` | `POST /cp/seats/release` |

The pharmacy still mints and custodies its own `license_key` in the tenant DB
(`sync.device_licenses`); `licensing-master` only accounts the seat against the
batch token's quota so the portal shows an accurate device list.

Leave the two vars empty and the in-process dev stub stays in place — no code
change, tests unaffected.

---

## 6. Ongoing operator flow

1. New client → portal → product tab → **Crear cliente** (sets seat cap).
2. **Generar token** (batch token, choose the quota = how many terminals it may
   activate). Hand the `BATCH-...` string to the client's installer.
3. Client activates terminals; seats tick up on the subscription bar.
4. **Registrar pago** each cycle → pushes `valid_until` by `window_days` and
   flips the subscription back to paid/ACTIVE.
5. Missed payment → subscription goes `PAST_DUE`; after `grace_days` past
   `valid_until` the device state becomes `EXPIRED` and the client hits the
   lockout screen. **Suspender cliente** is the hard stop.
6. Lost/retired terminal → **Revocar** on the device row (frees the seat).

## 7. Backups

Only `licensing-db` holds state. `pg_dump` it on a schedule:

```bash
docker compose -f docker-compose.licensing.yml exec licensing-db \
  pg_dump -U postgres sistema_farmacia_licensing | gzip > licensing-$(date +%F).sql.gz
```
