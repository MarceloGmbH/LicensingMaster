# Deploying `licensing-master`

The central licensing control plane (ADR-0013). One deployable: Postgres +
FastAPI (`licensing-master`, portal bundled at `/`). Ingress is the shared
Traefik; a `cloudflared` tunnel connector is available but dormant
(`--profile tunnel`).

Two hostnames:

| Hostname | Serves | Auth |
|---|---|---|
| `licensing.alanadev.com` | the portal + `/admin/*` | **native login** (password + mandatory TOTP); public, DNS-only (no Cloudflare proxy) |
| `licensing-cp.alanadev.com` | `/cp/*` and `/activate` only | **service token** / batch token (product backends, first-run clients) |

> **The admin host is public by operator decision (2026-10-03).** The portal is
> the business's master key (mints licences, revokes devices, deletes tenants),
> so the native login is its only barrier: argon2id password + mandatory TOTP,
> per-email and per-IP lockout, CSRF header, audited logins. Keep the DNS record
> **DNS-only (grey cloud)**: behind the Cloudflare proxy the app sees Cloudflare
> IPs and the per-IP lockout stops working. To restrict it later, attach a
> Traefik `ipAllowList` to its router (the vm-gateway firewall must then also
> accept 443 from those networks). The `-cp` host stays public: machines call
> it and have no browser session.

`/cp/*` cannot sit behind a login wall: product backends authenticate with a
bearer service token that `licensing-master` hashes and checks against
`licensing.service_tokens`.

---

## 1. Bring up the stack (on the box, no public IP needed)

```bash
cp .env.licensing.example .env.licensing
# fill in: LM_POSTGRES_PASSWORD, LM_ED25519_PRIVATE_KEY_B64
#          (+ CF_TUNNEL_TOKEN only if you use the tunnel profile, step 2)

docker compose --env-file .env.licensing -f docker-compose.licensing.yml up -d --build
```

`entrypoint.sh` runs `alembic upgrade head` on start, so the schema and the
`farmacia` / `hotel` / `pos_pub` product rows are seeded automatically. The
`licensing-master` container listens on `:8000`; it is published on
`LM_BIND_ADDR:LM_HOST_PORT` for Traefik (or reached by `cloudflared` on the
internal `lm` network when the tunnel profile is used).

---

## 2. (Optional) Create the tunnel (Cloudflare dashboard)

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
you (both point at `<tunnel-id>.cfargotunnel.com`). (Only for the optional tunnel
profile; with Traefik, route both hostnames there instead.)

The app routes by path internally, so both hostnames can target the same
service — the network restriction in step 4 is what makes them behave differently.

---

## 4. Admin login (native password + TOTP) and network restriction

Cloudflare Access is no longer used. Do **not** create an Access application for
either hostname (an Access app on `-cp` would also break `/cp/*`).

**Client IP.** Both hostnames are DNS-only and reach Traefik through the VPS
HAProxy with PROXY protocol, so the app sees the real client IP (verified
2026-10-03). Keep `licensing-cp.alanadev.com` on a router that serves only
`/cp`, `/activate` and `/health`, and the admin router excluding those paths.

**Migrations** run automatically at container start (`entrypoint.sh` →
`alembic upgrade head`); revision `0003_admin_auth` creates
`licensing.admin_users` and `licensing.admin_sessions`.

**Bootstrap the first admin** (no admin exists after the upgrade, so the portal
cannot be used until you do this). Inside the running container:

```bash
docker exec -it <licensing-master-container> python -m src.cli create-admin --email you@example.com
```

It prompts for a password twice (min 12 characters), then prints the
`otpauth://` URI, an ASCII QR code and the base32 secret **once**. Scan the QR
with an authenticator app (or paste the secret). Log in at the portal with
email + password + the 6-digit code.

Other operator commands (`docker exec -it <container> python -m src.cli ...`):

| Command | Effect |
|---|---|
| `create-admin --email X` | new admin + TOTP enrolment |
| `reset-password --email X` | new password; revokes the admin's sessions |
| `reset-totp --email X` | new TOTP secret (lost phone); revokes sessions |
| `disable-admin --email X` / `enable-admin --email X` | deactivate / reactivate; disabling revokes sessions |
| `revoke-sessions --email X` | log the admin out everywhere |
| `list-admins` | accounts, state and last login (no secrets) |

Behaviour and settings (all `LM_ADMIN_*`, see `.env.example`):

- Sessions live in the database (`lm_session` cookie: HttpOnly, Secure unless
  `LM_APP_ENV` is a dev value, SameSite=Strict). Idle timeout 30 min (sliding),
  absolute 8 h.
- 5 failed logins per e-mail / 15 min lock that e-mail for 15 min; 10 per client
  IP likewise → `429 TOO_MANY_ATTEMPTS` + `Retry-After`. Wrong re-entered
  passwords on tenant deletion count the same way.
- A TOTP code can be used once (replay rejected).
- Every state-changing `/admin/*` request needs `X-Requested-With: lm-portal`
  (sent by the portal) in addition to SameSite=Strict.
- Deleting a tenant asks for the logged-in admin's **own** password. The old
  shared `LM_ADMIN_DELETE_PASSWORD` is gone.
- Audit actions: `admin.login`, `admin.login_failed`, `admin.logout` (admin
  email as actor; no password or code is ever logged), plus `admin.*` rows for
  CLI changes (actor `cli`).
- Removed settings: `LM_ACCESS_TEAM_DOMAIN`, `LM_ACCESS_AUD`, `LM_ADMIN_EMAILS`,
  `LM_ADMIN_DELETE_PASSWORD`. Delete them from your `.env`.

### Verifying

- `https://licensing.alanadev.com` → login form; six bad logins for one email
  → the sixth returns `429 TOO_MANY_ATTEMPTS`; a POST without
  `X-Requested-With: lm-portal` → `403`.
- `curl -s https://licensing.alanadev.com/admin/auth/me` → `401` without a session.
- `curl https://licensing-cp.alanadev.com/health` → `{"status":"ok",...}` with
  no login wall; `curl .../cp/subscription` → `401` JSON (not HTML).

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


---

## Hardening notes (audit A3)

- **Signing key is mandatory in production.** Generate it once with
  `python licensing-master/scripts/gen_ed25519_keypair.py`; put the private value
  in `LM_ED25519_PRIVATE_KEY_B64` and the public value in the desktop build's
  `VITE_DEVICE_CONFIG_PUBKEY`. The container exits at startup if it is missing
  or malformed (unless `LM_APP_ENV=development`).
- **Client IP for rate limiting.** The limiter keys on `request.client.host`,
  which uvicorn rewrites from `X-Forwarded-For` only when the TCP peer is in
  `--forwarded-allow-ips` (`LM_FORWARDED_ALLOW_IPS`, default `*`). Traefik gets
  the real IP via PROXY protocol from HAProxy and must **overwrite** (not trust
  client-supplied) `X-Forwarded-For`. Set `LM_FORWARDED_ALLOW_IPS` to Traefik's
  address/CIDR so uvicorn takes the rightmost untrusted hop; with `*` it takes
  the leftmost entry, which a client can forge if the proxy appends to a
  client-supplied header. If Traefik's IP is not trusted, every client shares
  Traefik's IP and one attacker can lock out everyone.
- Tenant `base_url` must be `https://` outside development.
