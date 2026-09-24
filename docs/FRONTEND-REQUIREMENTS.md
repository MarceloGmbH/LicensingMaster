# Frontend & Client Integration Requirements

**Audience:** whoever builds or retrofits an alanadev product's **client
application** (the thing a pharmacy/hotel/etc. actually installs) so it enforces
the licensing contract in `docs/INTEGRATION.md` without leaking licensing
concerns into business/domain code.

This document does **not** repeat the server contract — read
`docs/INTEGRATION.md` first. It covers the layer that document deliberately
leaves out: what stack the client needs, what the installer must do, where
secrets live on disk, and where in a real codebase this logic belongs.

**Reference implementation:** `apps/desktop-pos` ("PuntoFarma") in the
`SistemaFarmaciaV2` repo. Every "confirmed" claim below was read directly out
of that source tree, not assumed. Sections say explicitly when something is a
**recommendation** rather than something the reference project already does —
the reference has real gaps, and this document does not pretend otherwise.

---

## 1. Is Tauri mandatory?

**No — but a native shell capable of running signed native code is, and Tauri
is the only stack alanadev has proven this pattern on.**

`licensing-master`'s contract (§1 of INTEGRATION.md) is plain HTTP/JSON — it
does not require any particular client runtime. But the anti-piracy guarantee
this whole system exists for depends on two things a pure browser tab cannot
do:

1. Read a **hardware identifier the user cannot trivially spoof or copy**
   (motherboard/BIOS UUID, machine-id, primary MAC) — JavaScript in a webview
   has no API for this; it needs native code.
2. **Verify an Ed25519 signature** over the signed `device_config` payload at
   startup using a public key baked into the build — doable in a browser via
   WebCrypto, but only meaningfully tamper-resistant if the code doing the
   check, and the key it checks against, ship inside a compiled binary rather
   than editable web assets.

Confirmed reference stack (`apps/desktop-pos/package.json`,
`apps/desktop-pos/src-tauri/{Cargo.toml,tauri.conf.json}`):

- **Tauri v2** (`tauri = "2"`, `@tauri-apps/api ^2`), product name `PuntoFarma`,
  identifier `com.alanadev.puntofarma`, bundle `targets: "all"` (Tauri's
  bundler builds every installer format supported on the host OS — MSI/NSIS on
  Windows, `.dmg`/`.app` on macOS, `.deb`/AppImage on Linux — from one config).
- React 18 + Vite + TypeScript, Zustand for local UI state, TanStack Query for
  server state, react-router-dom for navigation.
- The Rust side (`src-tauri/src/lib.rs`) exposes exactly one custom command
  today, `hardware_uuid`, built from the `machine-uid` + `mac_address` + `sha2`
  crates — this is the literal implementation of
  `"hw-" + sha256(machine_uid + primary_MAC)` that INTEGRATION.md §5.1 cites as
  the reference approach.

**If a new product's client is not a locally-installed, per-device desktop
app** (e.g. it's a hosted multi-tenant web app with no per-install licensing
concept), this entire document doesn't apply — device-binding licensing is for
installed clients. For any product that *is* an installed desktop client,
**use Tauri** unless there's a concrete reason not to: it's the only stack this
pattern has been built and hardened against in production.

---

## 2. Installer responsibilities

The installer is not a separate binary here — it's the app's own first-run
screen, gated by a build-time flag (reference: `VITE_TERMINAL_MODE=1` for a
packaged build; unset for local dev, which skips licensing entirely).

### 2.1 Compute and persist `hardware_uuid`

- Computed **only** by native code (`#[tauri::command] fn hardware_uuid()` in
  the reference), exposed to the webview via `invoke("hardware_uuid")`.
- **Confirmed gap to watch for:** the reference's JS wrapper
  (`getHardwareUuid()` in `apps/desktop-pos/src/lib/terminalConfig.ts`) falls
  back to a **locally-generated random id** (`crypto.randomUUID()`) if the
  Tauri IPC call throws — this is a deliberate dev/browser-mode convenience,
  not a security hole in the packaged build (the native call always succeeds
  there), but copy this pattern carefully: never let that fallback path be
  reachable in a release build, or every unlicensed dev build looks like a
  distinct "device" to the server.

### 2.2 Activate against licensing-master directly

The reference calls licensing-master's **public** `POST /activate`
(INTEGRATION.md §4.10) straight from the Tauri frontend — not proxied through
the product's own backend — because at first boot the app doesn't know its
tenant's backend URL yet. This is exactly the scenario `/activate` is designed
for: the technician types a batch token and a device name, nothing else.

```
1. Technician types BATCH-<PRODUCT>-<TENANT>-<HEX8> + a device name.
2. App computes hardware_uuid natively.
3. POST https://licensing-cp.<domain>/activate
   { token, hardware_uuid, device_name }          [no auth — batch-token gated]
4. Response: { tenant_base_url, license_key, device_config, reactivated }
5. Persist all four fields locally (§2.3). Reload — the app now knows which
   backend to talk to for everything else.
```

Reference: `apps/desktop-pos/src/lib/terminalConfig.ts` (`centralActivate`),
`apps/desktop-pos/src/modules/setup/TerminalSetupView.tsx`.

**Never** ship a `service token` (`svc_...`) inside the client. The installer
only ever handles the batch token, which is quota-limited and safe to embed in
a distributed build; the service token stays server-side, in the product
backend's environment, forever.

### 2.3 Persist `device_config` / `license_key` — storage mechanism

**Confirmed (not a recommendation): the reference stores this in plain
`localStorage`.** The in-repo comment is explicit about it:

> "Persisted in localStorage — Tauri v2 webviews keep it in the app's own
> profile dir, so it survives restarts."

This is **not** OS-level secure storage (not Keychain, not Windows Credential
Manager, not a Stronghold vault) — it is a JSON blob on disk under the
webview's profile directory, readable by anything with filesystem access to
that user profile.

**Recommendation (not what the reference does — a real gap worth closing in
new work):** persist `device_config`/`license_key` in OS-level secure storage
instead:

- Tauri: [`tauri-plugin-stronghold`](https://v2.tauri.app/plugin/stronghold/)
  (an encrypted, password/key-derived vault file) or a keychain-backed plugin
  wrapping the OS credential store (macOS Keychain / Windows Credential
  Manager / Linux Secret Service via `libsecret`).
- Fallback, in that order, if the secure-storage plugin is unavailable on a
  target platform: an encrypted-at-rest file the app manages itself (key
  derived from the hardware fingerprint, so it isn't portable to another
  machine either) → the reference's plain `localStorage` pattern, as a last
  resort.

Why this matters less than it sounds, and why the reference's gap isn't
catastrophic: the file is **not** the thing enforcing anti-piracy by itself.
Copying `device_config` to another machine doesn't help an attacker, because
every subsequent `verify`/`heartbeat`/`status` call also sends that machine's
own `hardware_uuid`, which won't match the bound device. Secure storage is
defense-in-depth (stops casual license-key harvesting / social-engineering
reuse of a *pasted* license key, which the reference's own "Verificar Pago"
flow explicitly allows — see §5), not the primary control. Treat the signature
check below as load-bearing; treat storage hardening as a should-do upgrade.

### 2.4 Verify the signature on every load

Confirmed reference implementation (`verifyDeviceConfig` in
`terminalConfig.ts`): supports a dev signer (`DEVCFG:<sha256[:32]>`) and a
production signer (`ed25519:<base64 sig>`, checked via WebCrypto against a
public key baked into the build as `VITE_DEVICE_CONFIG_PUBKEY`). It
**fails open** (treats the config as valid) only when the runtime genuinely
cannot perform the check (no WebCrypto Ed25519 support, no configured public
key) — a deliberate "don't brick a real terminal over a browser API gap"
tradeoff, not a loophole to be exploited. Keep this fail-open scope narrow: it
should trigger only on capability absence, never on a verification that ran
and failed.

---

## 3. Startup / launch verification flow — the two-guard boundary pattern

This is the part the server contract can't tell you, and the part most likely
to get tangled into business logic if done casually. The reference solves it
with **exactly two mount points**, confirmed by reading
`apps/desktop-pos/src/App.tsx`:

### 3.1 Boot guard — before anything renders

```tsx
// App.tsx — before providers, before routing, before login
if (terminalMode() && !isActivated(loadTerminalConfig())) {
  return <TerminalSetupView />;
}
if (!isBooted) {
  return <LoginView />;
}
```

One `if`, at the very top of the root component, reading only **locally
persisted** activation state (`serverUrl` + `licenseKey` present) — no network
call is needed to decide whether to show the activation screen. This is the
first-run / not-yet-activated gate. It renders instead of the whole app, not
around it.

### 3.2 Session guard — mounted once, next to the routes, not around them

```tsx
<MainLayout ...>
  <LicenseGuard onOpenLicensing={() => navigateTo("settings")} />
  {route === "home" && <HomeView ... />}
  {/* ...every other route... */}
</MainLayout>
```

`LicenseGuard` (`apps/desktop-pos/src/modules/settings/LicenseGuard.tsx`) is a
sibling of the route switch, not a wrapper around it. It:

- reads the hardware fingerprint and queries the license status via
  react-query (`useLicenseStatus`, hitting the product's **own backend**'s
  `GET /licensing/status`, never licensing-master directly — see §4),
- renders `null` for `UNKNOWN` (an unactivated dev/browser build is never
  blocked — this matters, don't invert it),
- renders a dismissible amber banner for `GRACE` or "expiring soon",
- renders a full-screen blocking modal for `EXPIRED`/`REVOKED`.

Because it's a sibling, not a wrapper, it never intercepts navigation, never
owns route state, and every business view underneath renders completely
unaware that licensing exists.

### 3.3 Where this code lives — module placement

The reference spreads this across three conventional folders rather than one
dedicated module (`lib/` for infra, `modules/setup/` for first-run,
`modules/settings/` for the ongoing guard + admin UI). For a **new** build,
prefer consolidating it — it's a handful of files that are all one concern:

```
src/lib/licensing/
  native.ts        # Tauri invoke("hardware_uuid") wrapper + browser fallback
  config.ts        # persist/load/verify device_config + license_key
  api.ts            # calls to the product's own backend: /licensing/status, /heartbeat
  useLicenseStatus.ts / useHeartbeat.ts   # react-query hooks
  BootGate.tsx      # the §3.1 early-return component
  LicenseGuard.tsx  # the §3.2 sibling overlay component
```

Every business module imports **nothing** from `lib/licensing/` except the
app root mounting `BootGate` once and `LicenseGuard` once. That is the entire
integration surface.

**Explicitly do not:**

- call the login gate from inside an individual page/view/feature component,
- thread license state through business stores (cart, catalog, sales, auth),
- make any business API call wait on a client-side license check — the
  product backend enforces its own server-side gate independently
  (`verify_device_credential` in the reference backend calls licensing-master's
  `POST /cp/devices/verify` at login time); the client-side guard is UX, not
  the security boundary.

---

## 4. The client never holds the service token — one more backend hop

Confirmed architecture split in the reference (`apps/backend/src/modules/licensing/`):

- The **installer's** one-time `/activate` call goes straight to
  licensing-master (§2.2) — safe, because it's gated by a quota-limited batch
  token, not a service token.
- **Every ongoing call** (`GET /licensing/status`, `POST /licensing/heartbeat`)
  goes to the **product's own backend**, unauthenticated but keyed by
  `hardware_uuid` (+ `license_key` for heartbeat). The product backend is the
  only party holding `SF_LICENSING_SERVICE_TOKEN`, and it is the only thing
  allowed to call licensing-master's `/cp/*` endpoints on the device's behalf.

This is not optional plumbing — it's the reason the service token stays out of
every distributed binary. Any retrofit that has the frontend call
`/cp/devices/verify` or `/cp/devices/heartbeat` directly is a regression from
this pattern; put a thin passthrough in the product's own backend instead
(the reference's `apps/backend/src/modules/licensing/interfaces/router.py` is
that passthrough, plus its own locally-authoritative `ACTIVE`/`GRACE`/
`EXPIRED`/`REVOKED` state derived from its own `device_licenses` table and the
tenant's subscription — this is licensing-master's **Pattern B**, "product
backend mints its own keys," per INTEGRATION.md §5).

---

## 5. Scheduled heartbeat

**Confirmed gap: not implemented as a background task in the reference.**
`useHeartbeat()` exists (`apps/desktop-pos/src/modules/settings/hooks/useLicensing.ts`)
but is wired to a manual "Verificar Pago" button in the settings UI
(`LicensingManagerTab.tsx`), not to a timer. `security-licensing-testing.md`'s
own spec ("every 24 hours, or on POS startup, Tauri silently pings...") is
aspirational documentation that the current code doesn't yet fulfill.

**Recommendation for new/retrofit work** — implement this for real:

- One `setInterval`-driven effect, mounted once at the app root alongside the
  session guard (not per-view), that:
  - fires once shortly after boot (once activated),
  - then re-fires at most once per `min(grace_days, 1–3 days)` per
    INTEGRATION.md §6's re-check rule,
  - persists `last_heartbeat_at` locally so a relaunch inside the window
    doesn't immediately re-fire,
  - is a no-op (skips, doesn't error) while offline — let the retry/backoff
    and grace-cache rules in INTEGRATION.md §6 handle that, don't build a
    second offline policy here.
- If the heartbeat must survive the window being minimized/backgrounded for a
  long time, move the timer into the Rust side as a Tauri background task
  instead of a webview `setInterval` (webview timers can be throttled by the
  OS when unfocused); the JS layer only needs to read the result.

---

## 6. Offline-grace cache — reuse the app's existing local-first storage

INTEGRATION.md §6 is explicit: licensing-master has **no cache for the
client** — the client must persist its own last verdict and honor it offline
until `valid_until + grace_days`. The question this document exists to answer
is: *where does that live in a real client, without inventing a second local
database?*

**What the reference project has, confirmed:**

- A fully specified local-first SQLite schema for business data
  (`context/docs/architecture/database-sqlite.md`): WAL-mode SQLite embedded
  in the Tauri process, an FTS5 catalog cache, and outbox-style tables
  (`offline_sales`, `local_cash_shifts`) with a `pending_sync` flag — designed
  as the single embedded local-storage mechanism for everything the terminal
  needs offline.
- **But**, as of this reading, that layer is a designed target, not yet wired
  up: `src-tauri/src/lib.rs` implements only the `hardware_uuid` command: the
  Tauri commands the frontend already calls for it
  (`search_catalog_fts`, `print_escpos_receipt`, `kick_cash_drawer` in
  `apps/desktop-pos/src/lib/tauri-bridge.ts`) have no Rust-side implementation
  yet and currently fall back to stub/mock behavior. `useSyncStore.ts`'s
  online/offline indicator is likewise a UI-only status flag today, not backed
  by a real sync queue.
- Consequently, the license status check today has **no offline fallback at
  all** — `useLicenseStatus` is a plain react-query fetch with no persisted
  verdict, so it simply fails/goes stale if the product backend is
  unreachable.

**Recommendation, consistent with the project's own designed architecture:**
when that embedded SQLite layer exists (or as you build it), add **one more
table to the same database** rather than a second storage mechanism:

```sql
CREATE TABLE IF NOT EXISTS license_cache (
    hardware_uuid   TEXT PRIMARY KEY,
    state           TEXT NOT NULL,        -- ACTIVE | GRACE | EXPIRED | REVOKED | UNKNOWN
    valid_until     TEXT,
    days_remaining  INTEGER,
    grace_days      INTEGER NOT NULL,
    checked_at      TEXT NOT NULL         -- ISO-8601 UTC, for clock-rollback detection
);
```

Written on every successful `status`/`heartbeat` response; read by the
session guard (§3.2) exactly per INTEGRATION.md §6's rule:

```
if online:
    v = call /licensing/status (or /heartbeat); write license_cache; allow = v.valid
else:
    row = read license_cache
    allow = row exists
            and row.state in (ACTIVE, GRACE)
            and now() <= row.valid_until + row.grace_days
    if now() < row.checked_at:   # clock turned back
        allow = false; force an online re-check before continuing
```

**If the SQLite layer isn't built yet** (true for the reference project
today), don't block licensing work on it — fall back to exactly the mechanism
already used for `device_config`/`license_key`: one more key in the same
`localStorage` blob (`terminalConfig.ts`'s `TerminalConfig` shape is the right
place to add `lastKnownState` / `lastCheckedAt` fields). The point is: **one**
local persistence mechanism per app, reused for licensing, not a bespoke
second one.

---

## 7. Retrofit checklist — adding this to an EXISTING project, no business-logic changes

1. Add a native hardware-fingerprint command (Tauri: one `#[tauri::command]`,
   ~15 lines — see §1) and a thin bridge module calling it from JS.
2. Add one new frontend module (`lib/licensing/` per §3.3): native bridge,
   config persistence + signature verification, API client, react-query
   hooks, and the two guard components. Nothing here imports from or is
   imported by existing business modules except the two mount points below.
3. Add exactly **two integration points** in the app shell, and nowhere else:
   - one early-return **boot guard** before the router/providers mount (§3.1),
   - one sibling **session guard** component mounted once next to (not
     around) the route switch (§3.2).
4. Add a product-backend passthrough module (mirrors
   `apps/backend/src/modules/licensing/` — domain/application/infrastructure/
   interfaces) that holds the service token and proxies `verify`/`heartbeat`
   to licensing-master. This is new code, not a change to existing backend
   modules.
5. Add the scheduled heartbeat (§5) as one root-mounted effect or a native
   background task — not inside any existing feature.
6. Add the offline-grace cache (§6) — one new table or one new persisted-config
   field, using the app's existing local storage mechanism.
7. Wire the installer's first-run screen (§2) as a new top-level view, shown
   only by the boot guard's early return.

**Do NOT touch:** domain/business entities, existing API routes for business
resources, existing local-first sync logic for business data (only *add* a
table/field to it, never restructure it for this), global state stores for
business features (cart, catalog, sales, auth-permissions), or the routing
logic itself (the session guard is a sibling, never a wrapper).

---

## 8. New-project checklist — building this in from day one

1. Pick Tauri v2 (or an equivalent native-shell stack — see §1) before writing
   any business UI; hardware fingerprinting and signature verification are
   much cheaper to design in than to retrofit.
2. Design the local-first storage layer (embedded SQLite, or equivalent) for
   business data from the start, and give the license cache (§6) a seat in it
   from day one — don't let it become a second bolt-on mechanism later.
3. Structure licensing as its own module (`lib/licensing/` frontend,
   `modules/licensing/` backend) from the first commit, with the two-guard
   pattern (§3) as the only integration surface into the app shell.
4. Decide Pattern A vs. Pattern B (INTEGRATION.md §5) before writing the
   activation flow — it determines whether the backend needs its own signer
   and device-state table (Pattern B, the reference's choice) or whether
   licensing-master's signed `device_config` is the sole source of truth
   (Pattern A).
5. Implement the scheduled heartbeat and the offline-grace cache as real,
   tested code before first release — do not ship the reference's current gap
   (manual-only heartbeat, no offline fallback) as the starting point.
6. Use OS-level secure storage for `device_config`/`license_key` from the
   start (§2.3) — it costs little more up front than `localStorage` and
   avoids retrofitting it later.
