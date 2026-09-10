# Licensing Integration Guide

**Audience:** any alanadev product that needs to license its installations
against the central control plane. You do **not** need to read `licensing-master`
source to integrate — this document is the contract.

- **Base URL (control plane):** `https://licensing-cp.alanadev.com`
- **Envelope:** every response is `{ "meta": {...}, "data": <payload|null>, "errors": [...] }`
- **Auth:** `Authorization: Bearer <service-token>` on all `/cp/*`; the public
  `/activate` endpoint is unauthenticated and gated by a batch token.
- **Contract version:** licensing-master `1.0.0`, DB schema head `0002_tenant_base_url`.

---

## 1. What the service is and what it guarantees

`licensing-master` is a single FastAPI service over one Postgres database. It is
the authority for **which installations of which tenant may run**, and for
**how many** (seat accounting).

It guarantees:

- **A stable per-device verdict.** Given a `(hardware_uuid, license_key)` pair it
  returns one of a small set of states (§2) plus a day countdown.
- **Seat caps are enforced centrally.** A tenant's `seat_limit` and each batch
  token's `quota` are checked on activation; exceeding either is a hard `409`.
- **Time-boxed validity with a grace tail.** Every device has a `valid_until`.
  After it passes, the device is in `GRACE` for `grace_days`, then `EXPIRED`.
- **Idempotent activation.** Re-activating a `hardware_uuid` that already holds a
  live seat refreshes it and consumes no new seat.
- **An operator audit trail** for every tenant/subscription/device mutation.

It does **not**:

- Hold any product data (inventory, sales, users — those live in each product's
  own tenant DB).
- Push anything to clients. Clients pull (`/activate`, then periodic
  `heartbeat` / `status`).
- Provide its own offline cache for your backend — see §6.

---

## 2. License model

### Entities

| Entity | Key fields | Meaning |
|---|---|---|
| **product** | `code` (`farmacia`, `hotel`, `pos_pub`) | one alanadev product line |
| **tenant** | `slug`, `name`, `base_url`, `status` | one client company on one product. `status` ∈ `ACTIVE` / `SUSPENDED` / `ARCHIVED` |
| **subscription** | `seat_limit`, `window_days`, `grace_days`, `valid_until`, `status`, `is_paid` | exactly one per tenant. `status` ∈ `ACTIVE` / `PAST_DUE` / `SUSPENDED` |
| **batch token** | `token`, `quota`, `seats_consumed`, `expires_at`, `is_revoked` | an activation coupon handed to a client's installer. Activates up to `quota` devices |
| **device activation** | `hardware_uuid` (unique), `device_name`, `license_key`, `valid_from`, `valid_until`, `is_revoked`, `last_seen_at` | one licensed installation |
| **service token** | `token_hash`, `product_id`, `tenant_id`, `is_revoked` | a product backend's credential for `/cp/*` |

### Device state machine

State is derived on every read from `valid_until`, `grace_days` and `is_revoked`:

```
days_remaining = floor((valid_until - now) / 1 day)

is_revoked                       -> REVOKED     (terminal until un-revoked by operator)
days_remaining >= 0              -> ACTIVE
-grace_days <= days_remaining<0  -> GRACE       (still allowed to run; warn the user)
days_remaining < -grace_days     -> EXPIRED     (block)
no activation row for the uuid   -> UNKNOWN
```

`verify` (§4.8) treats **`ACTIVE` and `GRACE` as `valid: true`**; everything else
is `valid: false`.

### How validity moves

- **Activation** sets `valid_until = now + window_days`.
- **Heartbeat while paid** (`subscription.is_paid` **and** `tenant.status == ACTIVE`)
  pushes `valid_until = now + window_days` again — a paid device that heartbeats
  never expires.
- **Heartbeat while unpaid** updates only `last_seen_at`. The countdown runs out
  → `GRACE` → `EXPIRED`.
- **Operator records a payment** → `valid_until += window_days`, subscription back
  to `is_paid = true`, `status = ACTIVE`.
- **Operator suspends the tenant** → subscription `SUSPENDED`, `is_paid = false`;
  devices fall to `GRACE`/`EXPIRED` as their clocks run out. Hard stop.
- **Operator revokes a device** → `REVOKED` immediately, seat freed.

---

## 3. Authentication

### 3.1 Service token — your backend → `/cp/*`

- **Format:** `svc_` + 43 url-safe base64 chars. Example shape:
  `svc_YUgVmkK6uVC3RMSBmrQzHSxt5msWExkveOYs0JnSktg`.
- **Scope:** bound to one `(product, tenant)`. Every `/cp/*` call it makes is
  implicitly that tenant — you never pass a tenant id.
- **Transport:** `Authorization: Bearer svc_...`. TLS only. Never in a URL or log.
- **Obtain:** the operator, in the portal → open the tenant → **Emitir service
  token** → the raw value is shown **once**. (API: `POST
  /admin/tenants/{tenant_id}/service-tokens` with `{"name": "..."}` →
  `data.token`.)
- **Storage server-side:** only the SHA-256 hash is kept. A lost token cannot be
  recovered — mint a new one.
- **Rotation:** mint a new token, deploy it, then have the operator revoke the
  old one (`UPDATE licensing.service_tokens SET is_revoked = true WHERE
  service_token_id = …`). There is deliberately no self-service revoke endpoint.

### 3.2 Batch token — a client's installer → `/activate`

- **Format:** `BATCH-<PRODUCT>-<TENANT_SLUG>-<HEX8>`, e.g.
  `BATCH-FARMACIA-FARMADEMO-C001FD33`.
- **Scope:** one subscription. Carries the tenant identity, so `/activate` needs
  no other auth.
- **Quota:** activates up to `quota` distinct `hardware_uuid`s; `seats_consumed`
  ticks up. A revoked device hands its seat back.
- **Obtain:** operator, portal → tenant → **Generar token** (pick the quota).
  (API: `POST /admin/tenants/{tenant_id}/batch-tokens` with `{"quota": N,
  "expires_at": "<iso>"?, "note": "..."?}`.)
- **Lifetime:** optional `expires_at`; revoke any time via `POST
  /admin/batch-tokens/{id}/revoke`.
- **Sensitivity:** lower than a service token (quota-bounded, tenant-scoped, no
  `/cp/*` access) but still hand it over a private channel.

---

## 4. Control-plane endpoints (`/cp/*`)

All require `Authorization: Bearer <service-token>`. All responses are the
`{meta, data, errors}` envelope; the tables below describe `data`.

### 4.0 `GET /health` — liveness (no auth)

```bash
curl -s https://licensing-cp.alanadev.com/health
# {"status":"ok","service":"licensing-master"}
```

### 4.1 `GET /cp/subscription`

```bash
curl -s https://licensing-cp.alanadev.com/cp/subscription \
  -H "Authorization: Bearer $SVC"
```

```json
{ "state": "ACTIVE", "is_paid": true, "window_days": 30,
  "grace_days": 5, "seat_limit": 5, "valid_until": "2026-10-10T05:00:00Z" }
```

`is_paid` is already ANDed with `tenant.status == ACTIVE` — trust it as the single
"tenant may operate" flag. `state` ∈ `ACTIVE` / `PAST_DUE` / `SUSPENDED`.

### 4.2 `GET /cp/seat-limit`

```json
{ "seat_limit": 5 }
```

### 4.3 `POST /cp/activation-tokens/verify`

```bash
curl -s -X POST .../cp/activation-tokens/verify \
  -H "Authorization: Bearer $SVC" -H 'Content-Type: application/json' \
  -d '{"token":"BATCH-FARMACIA-FARMADEMO-C001FD33"}'
```

```json
{ "valid": true, "subscription_id": 2 }
```

`valid` is false if the token is unknown, revoked, exhausted, expired, or bound
to a different subscription than your service token's tenant.

### 4.4 `POST /cp/devices/activate` → `201`

Pattern A (licensing-master custodies the key). Books a seat **and** mints a
signed `device_config`.

Request:

```json
{ "hardware_uuid": "hw-9f3c…", "device_name": "Caja 1",
  "token": "BATCH-…", "branch_ref": "SUC-01" }
```

Response `data`:

```json
{ "device": { "device_activation_id": 12, "hardware_uuid": "hw-9f3c…",
              "license_key": "LIC-3s9…", "valid_from": "…Z", "valid_until": "…Z",
              "is_revoked": false },
  "tenant_slug": "farmademo",
  "tenant_base_url": "https://farmademo.alanadev.com",
  "license_key": "LIC-3s9…",
  "device_config": "{\"payload\":{…},\"signature\":\"ed25519:…\"}",
  "reactivated": false }
```

- `license_key` is `LIC-` + 43 url-safe chars. **Store it** — it is required for
  `heartbeat` and `verify`.
- `device_config` is a JSON string: `{"payload": {...}, "signature": "..."}`.
  Signature is `ed25519:<b64>` in production, or `DEVCFG:<sha256-32>` when the
  service runs without a signing key. Persist it verbatim; treat an unverifiable
  signature as "cannot confirm" rather than "invalid" if you have no public key.
- `reactivated: true` means the uuid already had a live seat — same key returned,
  no new seat consumed.
- Re-run with the same uuid any time to refresh `valid_until` (idempotent).

### 4.5 `POST /cp/devices/heartbeat`

```json
{ "hardware_uuid": "hw-9f3c…", "license_key": "LIC-3s9…" }
```

Response `data` (same shape as `status`):

```json
{ "hardware_uuid": "hw-9f3c…", "state": "ACTIVE", "days_remaining": 29,
  "valid_until": "…Z", "last_heartbeat_at": "…Z", "is_revoked": false }
```

Call on a schedule (daily is typical). Errors: `404 NOT_FOUND` (uuid not
activated), `403 FORBIDDEN` (`license_key` mismatch, or device revoked).

### 4.6 `GET /cp/devices/status?hardware_uuid=…`

Read-only; no `license_key` needed. Same `data` shape as heartbeat. An unknown
uuid returns `state: "UNKNOWN"`, `days_remaining: 0` (HTTP `200`, not `404`).

### 4.7 `POST /cp/devices/verify`

The login gate. "Is this exact `(hardware_uuid, license_key)` allowed to run
right now?"

```json
{ "hardware_uuid": "hw-9f3c…", "license_key": "LIC-3s9…" }
```

```json
{ "valid": true, "state": "ACTIVE", "valid_until": "…Z",
  "days_remaining": 29, "reason": null }
```

`valid` is true only for `state` ∈ {`ACTIVE`, `GRACE`}. On mismatch/unknown you
get `valid: false` with a `reason` and **HTTP `200`** (it is a verdict, not an
error).

### 4.8 `POST /cp/seats/consume` → `201`

Pattern B (your backend custodies its own key). Books a seat against a batch
token's quota; **no** `device_config` is minted. Idempotent per uuid.

```json
{ "token": "BATCH-…", "hardware_uuid": "hw-9f3c…",
  "device_name": "Caja 1", "branch_ref": "SUC-01" }
```

```json
{ "consumed": true, "reactivated": false, "hardware_uuid": "hw-9f3c…" }
```

`consumed: false, reactivated: true` → the uuid already held a live seat. `409
SEAT_LIMIT_REACHED` → tenant `seat_limit` or token `quota` hit. `403
ACTIVATION_TOKEN_INVALID` → bad/again revoked/expired/foreign token.

### 4.9 `POST /cp/seats/release`

```json
{ "hardware_uuid": "hw-9f3c…" }
```

```json
{ "released": true }
```

Frees the seat (decrements the batch token, revokes the activation row). Always
`200`, even if the uuid was already released — safe to call in a rollback path.

### 4.10 `POST /activate` — public first-run (no auth)

Same request/response as `4.4`, but **unauthenticated** — the batch token is the
only credential. This is what a fresh install calls before it has anything else.
Extra guard: `403 FORBIDDEN` if the tenant is not `ACTIVE`.

```bash
curl -s -X POST https://licensing-cp.alanadev.com/activate \
  -H 'Content-Type: application/json' \
  -d '{"token":"BATCH-FARMACIA-FARMADEMO-C001FD33",
       "hardware_uuid":"hw-9f3c…","device_name":"Caja 1"}'
```

### Error codes

| HTTP | `errors[0].code` | When |
|---|---|---|
| 400 | `VALIDATION_ERROR` | bad field value (`field` names it) |
| 401 | `UNAUTHORIZED` | missing/!bearer service token |
| 403 | `FORBIDDEN` | invalid/revoked service token, key mismatch, tenant not ACTIVE |
| 403 | `ACTIVATION_TOKEN_INVALID` | batch token unknown / revoked / exhausted / expired / foreign |
| 404 | `NOT_FOUND` | uuid not activated (heartbeat), tenant/subscription missing |
| 409 | `CONFLICT` | slug already exists (admin) |
| 409 | `SEAT_LIMIT_REACHED` | `seat_limit` or token `quota` reached |
| 422 | `SCHEMA_VALIDATION` | malformed body / missing required key |

---

## 5. First-run activation flow

Two shapes. Pick one per product.

### Pattern A — terminal / thick client (licensing-master holds the key)

```
1. Installer ships with a BATCH-… token (per client, quota = #terminals).
2. On first boot the app computes a stable hardware_uuid (see §5.1).
3. POST /activate { token, hardware_uuid, device_name }        [no auth]
   -> { tenant_base_url, license_key, device_config }
4. Persist all three locally (device_config verbatim).
   tenant_base_url tells the app which product server to talk to.
5. From now on the app authenticates its user against tenant_base_url,
   and on each launch (or daily) calls the product backend, which calls
   POST /cp/devices/verify { hardware_uuid, license_key } to gate login.
6. The product backend calls POST /cp/devices/heartbeat on a schedule to
   push valid_until forward while the tenant is paid.
```

### Pattern B — product backend that mints its own keys (today's pharmacy)

```
1. Operator issues a service token for the tenant -> product backend env:
   SF_LICENSING_BASE_URL, SF_LICENSING_SERVICE_TOKEN.
2. Backend generates + stores its own license_key per device in its tenant DB.
3. On device registration:
   POST /cp/activation-tokens/verify { token }        -> must be valid
   POST /cp/seats/consume { token, hardware_uuid }    -> books the seat
4. On login: backend checks its own key, and optionally calls
   POST /cp/devices/verify for the central verdict.
5. Rollback / device retirement: POST /cp/seats/release { hardware_uuid }.
```

### 5.1 hardware_uuid

Must be **stable across reboots and app reinstalls** and **distinct per physical
machine**. Reference approach (PuntoFarma desktop): `"hw-" +
sha256(machine_uid + primary_MAC)`. Length ≤ 150 chars. Treat it as opaque.

---

## 6. Failure handling

`licensing-master` is a network dependency. Design for it being briefly
unreachable — **not** for it being optional.

| Situation | What the client/backend should do |
|---|---|
| Connection error / timeout | Retry with backoff a few times, then fall back to the **last known state** (see below). Set a per-request timeout (5 s is the reference). |
| `409 SEAT_LIMIT_REACHED` | Non-retryable. Surface "seat limit reached" to the operator. |
| `403 ACTIVATION_TOKEN_INVALID` / `FORBIDDEN` | Non-retryable. The token or key is bad — do not loop. |
| `5xx` / other `4xx` | Treat as "unavailable", same as a timeout. |
| `release_seat` fails | Ignore. It is advisory; never fail a rollback because of it. |

### Offline grace — where it lives

There is **no cache inside `licensing-master` for your backend**. Offline
tolerance is built from two pieces:

1. **The `GRACE` state.** A device stays `valid` for `grace_days` past
   `valid_until`. As long as your last successful `verify`/`status` said `ACTIVE`
   or `GRACE`, you may keep running until that cached `valid_until` + `grace_days`
   elapses in wall-clock time.
2. **Your local cache.** Persist the last successful
   `{ state, valid_until, days_remaining, checked_at }`. While the control plane
   is unreachable, honour that cached verdict until `valid_until + grace_days`,
   then hard-block. Re-check online at least once per `min(grace_days, 1–3 days)`.

Recommended client rule:

```
if online:
    v = verify(hardware_uuid, license_key); cache(v); allow = v.valid
else:
    allow = cache.exists
            and cache.state in (ACTIVE, GRACE)
            and now() <= cache.valid_until + grace_days
```

Guard against clock tampering: if `now()` jumps backwards vs the last
`checked_at`, treat the device as needing an online re-check before it runs.

---

## 7. Network requirements

- **`https://licensing-cp.alanadev.com`** must be reachable from: every product
  backend server, and from first-run clients (for `/activate`). Public DNS,
  TLS terminated at the shared Traefik. No Cloudflare Access on this hostname.
- **Never** put an Access application (or any login wall) in front of
  `licensing-cp` — `/cp/*` callers are machines and will choke on an HTML page.
- **`https://licensing.alanadev.com`** (portal + `/admin/*`) is operator-only and
  **must** stay behind Cloudflare Access. Do not integrate against `/admin/*`.
- The `licensing-db` Postgres is never published — internal Docker network only.
- Service tokens and batch tokens are bearer secrets: TLS only, out of URLs and
  logs, rotate on suspicion of leak.

---

## 8. Integration checklist for a new product

- [ ] Operator creates the product row (`farmacia` / `hotel` / `pos_pub` exist;
      a new product line is a one-row insert + a migration).
- [ ] Operator creates the tenant (sets `seat_limit`, `base_url`).
- [ ] Decide Pattern A or B (§5).
- [ ] Pattern B: operator mints a **service token**; put
      `SF_LICENSING_BASE_URL` + `SF_LICENSING_SERVICE_TOKEN` in the backend env.
- [ ] Pattern A: operator generates a **batch token** per client; ship it in the
      installer.
- [ ] Implement a stable `hardware_uuid` (§5.1).
- [ ] Wire activation (`/activate` or `/cp/seats/consume`).
- [ ] Wire the login gate (`/cp/devices/verify`) and a scheduled
      `heartbeat`.
- [ ] Implement the offline cache + grace rule (§6).
- [ ] Handle `SEAT_LIMIT_REACHED` and `ACTIVATION_TOKEN_INVALID` as terminal,
      user-visible outcomes.
- [ ] Never expose `/admin/*`; verify `curl …/cp/subscription` returns JSON `401`
      (not HTML) from your environment.

---

## 9. Reference client (Python, ~40 lines)

A minimal Pattern-A client. Mirrors
`apps/backend/src/modules/licensing/infrastructure/http_control_plane.py` in the
SistemaFarmacia repo, trimmed to the essentials.

```python
import time, httpx

BASE = "https://licensing-cp.alanadev.com"

class LicensingClient:
    def __init__(self, service_token: str, timeout: float = 5.0):
        self._h = {"Authorization": f"Bearer {service_token}"}
        self._t = timeout

    def _call(self, method, path, **kw):
        try:
            r = httpx.request(method, f"{BASE}{path}", headers=self._h,
                              timeout=self._t, **kw)
        except httpx.HTTPError as e:
            raise Unavailable(str(e)) from e
        body = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        if r.status_code >= 400:
            code = (body.get("errors") or [{}])[0].get("code")
            if code == "SEAT_LIMIT_REACHED" or r.status_code == 409:
                raise SeatLimit()
            if code == "ACTIVATION_TOKEN_INVALID" or r.status_code in (401, 403):
                raise TokenInvalid()
            raise Unavailable(f"{r.status_code}")
        return body.get("data") or {}

    def subscription(self):        return self._call("GET", "/cp/subscription")
    def verify_token(self, tok):   return self._call("POST", "/cp/activation-tokens/verify", json={"token": tok})["valid"]
    def activate(self, tok, hw, name):
        return self._call("POST", "/cp/devices/activate",
                          json={"token": tok, "hardware_uuid": hw, "device_name": name})
    def verify_device(self, hw, key):
        return self._call("POST", "/cp/devices/verify",
                          json={"hardware_uuid": hw, "license_key": key})
    def heartbeat(self, hw, key):
        return self._call("POST", "/cp/devices/heartbeat",
                          json={"hardware_uuid": hw, "license_key": key})

class Unavailable(Exception): ...
class SeatLimit(Exception): ...
class TokenInvalid(Exception): ...

# --- offline-aware login gate -------------------------------------------
def may_run(client, hw, key, cache, grace_days):
    try:
        v = client.verify_device(hw, key)
        cache.put({"state": v["state"], "valid_until": v.get("valid_until"),
                   "checked_at": time.time()})
        return v["valid"]
    except Unavailable:
        c = cache.get()
        if not c or c["state"] not in ("ACTIVE", "GRACE"):
            return False
        # honour the cached verdict until valid_until + grace_days
        from datetime import datetime, timedelta, timezone
        vu = datetime.fromisoformat(c["valid_until"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) <= vu + timedelta(days=grace_days)
```

Full contract test: `licensing-master/tests/test_smoke.py` in this repo exercises
the whole lifecycle end to end.
