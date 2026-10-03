"""Native admin authentication: password + TOTP login, server-side sessions,
CSRF header, brute-force lockout, audit trail and own-password tenant deletion.

Run: LM_DATABASE_URL=postgresql+psycopg://... APP_ENV=test python -m pytest tests -q
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from tests.conftest import CSRF, AdminAccount, make_admin

pytestmark = pytest.mark.skipif(
    not os.getenv("LM_DATABASE_URL"), reason="LM_DATABASE_URL not set"
)


_TENANT_BODY = {
    "product_code": "farmacia",
    "name": "Auth Co",
    "seat_limit": 1,
    "base_url": "auth.example.com",
}


def _login(c: TestClient, a: AdminAccount, *, code: str | None = None, **over):
    body = {"email": a.email, "password": a.password, "totp_code": code or a.code()}
    body.update(over)
    return c.post("/admin/auth/login", json=body)


def _db():
    from src.db import engine

    return engine().begin()


def _now():
    from src.admin_auth import utcnow

    return utcnow()


def _session_row(admin: AdminAccount) -> dict:
    from src.tables import admin_sessions, admin_users

    with _db() as conn:
        return dict(
            conn.execute(
                select(admin_sessions)
                .join(admin_users, admin_users.c.admin_user_id == admin_sessions.c.admin_user_id)
                .where(admin_users.c.email == admin.email)
                .order_by(admin_sessions.c.admin_session_id.desc())
            )
            .mappings()
            .first()
        )


def _patch_session(admin: AdminAccount, **values) -> None:
    from src.tables import admin_sessions

    row = _session_row(admin)
    with _db() as conn:
        conn.execute(
            update(admin_sessions)
            .where(admin_sessions.c.admin_session_id == row["admin_session_id"])
            .values(**values)
        )


def _audit(action: str, actor: str) -> list[dict]:
    from src.tables import audit_log

    with _db() as conn:
        rows = conn.execute(
            select(audit_log).where(audit_log.c.action == action, audit_log.c.actor_email == actor)
        ).mappings()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- login


def test_login_sets_hardened_cookie(anon_client: TestClient, admin: AdminAccount) -> None:
    r = _login(anon_client, admin)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["email"] == admin.email
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("lm_session=")
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # APP_ENV=test is a dev environment
    assert r.headers["cache-control"] == "no-store"


def test_cookie_is_secure_outside_dev(
    anon_client: TestClient, admin: AdminAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    r = _login(anon_client, admin)
    assert r.status_code == 200, r.text
    assert "secure" in r.headers["set-cookie"].lower()


def test_only_the_token_hash_is_stored(anon_client: TestClient, admin: AdminAccount) -> None:
    r = _login(anon_client, admin)
    raw = r.cookies.get("lm_session") or anon_client.cookies.get("lm_session")
    assert raw and len(raw) >= 43  # 256 bits, urlsafe base64
    row = _session_row(admin)
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in json.dumps(row, default=str)


def test_login_is_case_insensitive_on_email_and_updates_last_login(
    anon_client: TestClient, admin: AdminAccount
) -> None:
    from src.tables import admin_users

    assert _login(anon_client, admin, email=admin.email.upper()).status_code == 200
    with _db() as conn:
        last = conn.execute(
            select(admin_users.c.last_login_at).where(admin_users.c.email == admin.email)
        ).scalar_one()
    assert last is not None


def _failure_body(r) -> tuple[int, dict]:
    assert "set-cookie" not in r.headers
    return r.status_code, r.json()["errors"]


def test_all_login_failures_look_identical(anon_client: TestClient, admin: AdminAccount) -> None:
    from src.tables import admin_users

    inactive = make_admin(f"off-{uuid.uuid4().hex[:6]}@alanadev.com")
    with _db() as conn:
        conn.execute(update(admin_users).where(admin_users.c.email == inactive.email).values(is_active=False))
    outcomes = [
        _failure_body(_login(anon_client, admin, password="wrong password here")),
        _failure_body(_login(anon_client, admin, code="000000")),
        _failure_body(_login(anon_client, admin, code="abc")),
        _failure_body(_login(anon_client, admin, email="ghost@alanadev.com")),
        _failure_body(_login(anon_client, inactive)),
    ]
    assert {o[0] for o in outcomes} == {401}
    assert len({json.dumps(o[1], sort_keys=True) for o in outcomes}) == 1
    assert outcomes[0][1][0]["code"] == "UNAUTHORIZED"


def test_totp_code_cannot_be_replayed(anon_client: TestClient, admin: AdminAccount) -> None:
    code = admin.code()
    assert _login(anon_client, admin, code=code).status_code == 200
    anon_client.post("/admin/auth/logout")
    assert _login(anon_client, admin, code=code).status_code == 401
    assert _login(anon_client, admin, code=admin.code(steps=1)).status_code == 200


def test_old_totp_step_is_rejected_after_a_newer_one_was_used(
    anon_client: TestClient, admin: AdminAccount
) -> None:
    assert _login(anon_client, admin, code=admin.code(steps=1)).status_code == 200
    assert _login(anon_client, admin, code=admin.code(steps=-1)).status_code == 401


def test_failed_password_does_not_burn_the_totp_step(anon_client: TestClient, admin: AdminAccount) -> None:
    code = admin.code()
    assert _login(anon_client, admin, code=code, password="wrong password here").status_code == 401
    assert _login(anon_client, admin, code=code).status_code == 200


# ---------------------------------------------------------------- sessions


def test_me_requires_a_session(anon_client: TestClient) -> None:
    assert anon_client.get("/admin/auth/me").status_code == 401
    anon_client.cookies.set("lm_session", "not-a-real-token")
    assert anon_client.get("/admin/auth/me").status_code == 401
    assert anon_client.get("/admin/products").status_code == 401


def test_cloudflare_and_dev_headers_no_longer_authenticate(anon_client: TestClient) -> None:
    for h in (
        {"X-Dev-Admin-Email": "tester@alanadev.com"},
        {"Cf-Access-Jwt-Assertion": "a.b.c"},
    ):
        assert anon_client.get("/admin/products", headers=h).status_code == 401
    anon_client.cookies.set("CF_Authorization", "a.b.c")
    assert anon_client.get("/admin/products").status_code == 401


def test_me_returns_email(client: TestClient, admin: AdminAccount) -> None:
    assert client.get("/admin/auth/me").json()["data"] == {"email": admin.email}


def test_logout_revokes_session_and_clears_cookie(client: TestClient, admin: AdminAccount) -> None:
    raw = client.cookies.get("lm_session")
    r = client.post("/admin/auth/logout")
    assert r.status_code == 200
    assert "lm_session=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert _session_row(admin)["revoked_at"] is not None
    client.cookies.set("lm_session", raw)  # replaying the old token must fail
    assert client.get("/admin/auth/me").status_code == 401


def test_logout_without_session_is_harmless(anon_client: TestClient) -> None:
    assert anon_client.post("/admin/auth/logout").status_code == 200


def test_idle_timeout_is_sliding(client: TestClient, admin: AdminAccount) -> None:
    from src.config import settings

    idle = timedelta(seconds=settings.LM_ADMIN_SESSION_IDLE_SECONDS)
    _patch_session(admin, last_seen_at=_now() - idle + timedelta(seconds=60))
    assert client.get("/admin/auth/me").status_code == 200
    assert _now() - _session_row(admin)["last_seen_at"] < timedelta(seconds=30)  # slid forward
    _patch_session(admin, last_seen_at=_now() - idle - timedelta(seconds=1))
    assert client.get("/admin/auth/me").status_code == 401


def test_absolute_timeout_ignores_activity(client: TestClient, admin: AdminAccount) -> None:
    _patch_session(admin, expires_at=_now() - timedelta(seconds=1), last_seen_at=_now())
    assert client.get("/admin/auth/me").status_code == 401


def test_session_lifetimes_follow_settings(anon_client: TestClient, admin: AdminAccount) -> None:
    from src.config import settings

    _login(anon_client, admin)
    row = _session_row(admin)
    life = row["expires_at"] - row["created_at"]
    assert abs(life.total_seconds() - settings.LM_ADMIN_SESSION_ABSOLUTE_SECONDS) < 5
    assert settings.LM_ADMIN_SESSION_IDLE_SECONDS == 1800
    assert settings.LM_ADMIN_SESSION_ABSOLUTE_SECONDS == 8 * 3600


def test_revoked_session_is_rejected(client: TestClient, admin: AdminAccount) -> None:
    _patch_session(admin, revoked_at=_now())
    assert client.get("/admin/auth/me").status_code == 401


def test_deactivating_the_user_kills_live_sessions(client: TestClient, admin: AdminAccount) -> None:
    from src.tables import admin_users

    with _db() as conn:
        conn.execute(update(admin_users).where(admin_users.c.email == admin.email).values(is_active=False))
    assert client.get("/admin/auth/me").status_code == 401


# ---------------------------------------------------------------- CSRF


def test_state_changing_admin_requests_need_the_csrf_header(client: TestClient) -> None:
    from src.main import app

    bare = TestClient(app)
    bare.cookies.update(client.cookies)
    assert bare.get("/admin/products").status_code == 200  # safe methods are exempt
    assert bare.post("/admin/tenants", json={}).status_code == 403
    assert bare.patch("/admin/tenants/1", json={}).status_code == 403
    assert bare.request("DELETE", "/admin/tenants/1", json={"admin_password": "x"}).status_code == 403
    assert bare.post("/admin/auth/logout").status_code == 403
    wrong = bare.post("/admin/tenants", json={}, headers={"X-Requested-With": "XMLHttpRequest"})
    assert wrong.status_code == 403
    assert wrong.json()["errors"][0]["code"] == "FORBIDDEN"
    # with the header the same session is accepted (validation error, not 403)
    ok = bare.post("/admin/tenants", headers=CSRF, json=_TENANT_BODY | {"slug": f"t{uuid.uuid4().hex[:8]}"})
    assert ok.status_code == 201, ok.text


def test_login_itself_needs_the_csrf_header(admin: AdminAccount) -> None:
    from src.main import app

    r = TestClient(app).post(
        "/admin/auth/login",
        json={"email": admin.email, "password": admin.password, "totp_code": admin.code()},
    )
    assert r.status_code == 403


def test_cp_and_activate_are_not_subject_to_the_csrf_header(anon_client: TestClient) -> None:
    from src.main import app

    plain = TestClient(app)
    r = plain.post("/activate", json={"token": "nope", "hardware_uuid": "HW-123456", "device_name": "x"})
    assert r.json()["errors"][0]["code"] == "ACTIVATION_TOKEN_INVALID"  # reached the handler
    assert plain.get("/cp/subscription").status_code == 401


# ---------------------------------------------------------------- brute force


def test_email_lockout_blocks_even_correct_credentials(anon_client: TestClient, admin: AdminAccount) -> None:
    from src.config import settings

    for _ in range(settings.LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES):
        assert _login(anon_client, admin, password="wrong password here").status_code == 401
    r = _login(anon_client, admin)
    assert r.status_code == 429
    assert r.json()["errors"][0]["code"] == "TOO_MANY_ATTEMPTS"
    assert int(r.headers["retry-after"]) > 0
    # another account from the same IP is unaffected
    other = make_admin(f"o-{uuid.uuid4().hex[:6]}@alanadev.com")
    assert _login(anon_client, other).status_code == 200


def test_lockout_also_applies_to_unknown_emails(anon_client: TestClient, admin: AdminAccount) -> None:
    from src.config import settings

    for _ in range(settings.LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES):
        _login(anon_client, admin, email="ghost@alanadev.com")
    assert _login(anon_client, admin, email="ghost@alanadev.com").status_code == 429


def test_ip_lockout_spans_emails(anon_client: TestClient, admin: AdminAccount) -> None:
    from src import ratelimit

    ratelimit.login_ip_limiter.max_failures = 3
    for i in range(3):
        _login(anon_client, admin, email=f"nobody{i}@alanadev.com")
    r = _login(anon_client, admin)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_successful_login_clears_the_email_counter(anon_client: TestClient, admin: AdminAccount) -> None:
    from src.config import settings

    n = settings.LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES - 1
    for _ in range(n):
        _login(anon_client, admin, password="wrong password here")
    assert _login(anon_client, admin).status_code == 200
    anon_client.post("/admin/auth/logout")
    for _ in range(n):
        assert _login(anon_client, admin, password="wrong password here").status_code == 401


# ---------------------------------------------------------------- audit


def test_login_logout_and_failures_are_audited(anon_client: TestClient) -> None:
    admin = make_admin(f"aud-{uuid.uuid4().hex[:8]}@alanadev.com")  # fresh actor: no stale rows
    bad_code = "123456" if admin.code() != "123456" else "654321"
    _login(anon_client, admin, password="hunter2 hunter2 hunter2", code=bad_code)
    assert _login(anon_client, admin).status_code == 200
    anon_client.post("/admin/auth/logout")

    assert len(_audit("admin.login", admin.email)) == 1
    assert len(_audit("admin.logout", admin.email)) == 1
    failed = _audit("admin.login_failed", admin.email)
    assert len(failed) == 1
    dump = json.dumps(failed[0]["payload"])
    assert "hunter2" not in dump and bad_code not in dump and admin.password not in dump


def test_unknown_email_failure_is_audited_too(anon_client: TestClient, admin: AdminAccount) -> None:
    _login(anon_client, admin, email="ghost@alanadev.com")
    assert len(_audit("admin.login_failed", "ghost@alanadev.com")) >= 1


# ---------------------------------------------------------------- tenant deletion


def _new_tenant(client: TestClient) -> int:
    r = client.post("/admin/tenants", json=_TENANT_BODY | {"slug": f"t{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 201, r.text
    return r.json()["data"]["tenant"]["tenant_id"]


def test_delete_tenant_requires_the_admins_own_password(client: TestClient, admin: AdminAccount) -> None:
    tid = _new_tenant(client)
    assert client.request("DELETE", f"/admin/tenants/{tid}").status_code == 403
    r = client.request("DELETE", f"/admin/tenants/{tid}", json={"admin_password": "wrong password here"})
    assert r.status_code == 403
    assert client.get(f"/admin/tenants/{tid}").status_code == 200  # still there
    # the old shared-secret header is gone
    r = client.request("DELETE", f"/admin/tenants/{tid}", headers={"X-Admin-Delete-Password": admin.password})
    assert r.status_code == 403
    ok = client.request("DELETE", f"/admin/tenants/{tid}", json={"admin_password": admin.password})
    assert ok.status_code == 200, ok.text
    assert client.get(f"/admin/tenants/{tid}").status_code == 404
    assert len(_audit("tenant.delete", admin.email)) >= 1


def test_another_admins_password_does_not_delete(client: TestClient) -> None:
    other = make_admin(f"p-{uuid.uuid4().hex[:6]}@alanadev.com", "another long password!")
    tid = _new_tenant(client)
    r = client.request("DELETE", f"/admin/tenants/{tid}", json={"admin_password": other.password})
    assert r.status_code == 403


def test_password_guessing_on_delete_is_throttled(client: TestClient, admin: AdminAccount) -> None:
    from src.config import settings

    tid = _new_tenant(client)
    for _ in range(settings.LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES):
        client.request("DELETE", f"/admin/tenants/{tid}", json={"admin_password": "wrong password here"})
    r = client.request("DELETE", f"/admin/tenants/{tid}", json={"admin_password": admin.password})
    assert r.status_code == 429


def test_delete_password_setting_is_gone() -> None:
    from src.config import settings

    for name in ("LM_ADMIN_DELETE_PASSWORD", "LM_ACCESS_TEAM_DOMAIN", "LM_ACCESS_AUD", "LM_ADMIN_EMAILS"):
        assert not hasattr(settings, name)
