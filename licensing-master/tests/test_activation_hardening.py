"""Audit A3: batch-token entropy, cross-tenant key leak, key rotation on
reactivation, and brute-force throttling of the public /activate.

Run: LM_DATABASE_URL=postgresql+psycopg://... APP_ENV=test python -m pytest tests -q
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("LM_DATABASE_URL"), reason="LM_DATABASE_URL not set"
)

H = {"X-Dev-Admin-Email": "tester@alanadev.com"}


@pytest.fixture()
def client() -> TestClient:
    from src.main import app

    return TestClient(app)


def _tenant(client: TestClient, *, seat_limit: int = 5, quota: int = 10) -> dict:
    slug = f"t{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/admin/tenants",
        headers=H,
        json={"product_code": "farmacia", "slug": slug, "name": "Hardening Co",
              "seat_limit": seat_limit, "base_url": "hardening.example.com"},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["data"]["tenant"]["tenant_id"]
    tok = client.post(f"/admin/tenants/{tid}/batch-tokens", headers=H, json={"quota": quota}).json()["data"]
    svc = client.post(f"/admin/tenants/{tid}/service-tokens", headers=H, json={"name": "h"}).json()["data"]["token"]
    return {"tid": tid, "token": tok["token"], "batch_token_id": tok["batch_token_id"],
            "S": {"Authorization": f"Bearer {svc}"}}


def _hw() -> str:
    return f"HW-{uuid.uuid4().hex[:12].upper()}"


def _activate(client: TestClient, token: str, hw: str):
    return client.post("/activate", json={"hardware_uuid": hw, "device_name": "Caja", "token": token})


def _seats(client: TestClient, t: dict) -> int:
    toks = client.get(f"/admin/tenants/{t['tid']}", headers=H).json()["data"]["batch_tokens"]
    return next(x["seats_consumed"] for x in toks if x["batch_token_id"] == t["batch_token_id"])


def _audit_actions(client: TestClient, hw: str) -> list[str]:
    items = client.get("/admin/audit", headers=H, params={"limit": 500}).json()["data"]["items"]
    return [a["action"] for a in items if str(a["target_id"]) == hw]


# ---- A: batch token entropy -------------------------------------------------


def test_batch_token_keeps_prefix_and_has_at_least_128_bits(client: TestClient) -> None:
    t = _tenant(client)
    m = re.fullmatch(r"BATCH-FARMACIA-[A-Z0-9-]+-([A-Za-z0-9_-]+)", t["token"])
    assert m, t["token"]
    # token_urlsafe(16) => 22 chars of base64url == 128 bits
    assert len(m.group(1)) >= 22


def test_batch_tokens_are_unique(client: TestClient) -> None:
    t = _tenant(client)
    other = client.post(f"/admin/tenants/{t['tid']}/batch-tokens", headers=H, json={"quota": 1}).json()["data"]["token"]
    assert other != t["token"]


# ---- B: reactivation ---------------------------------------------------------


def test_cross_tenant_reactivation_is_rejected_and_leaks_nothing(client: TestClient) -> None:
    a, b = _tenant(client), _tenant(client)
    hw = _hw()
    victim = _activate(client, b["token"], hw)
    assert victim.status_code == 201
    victim_key = victim.json()["data"]["license_key"]

    r = _activate(client, a["token"], hw)
    assert r.status_code == 409, r.text
    assert r.json()["errors"][0]["code"] == "DEVICE_BOUND_TO_OTHER_TENANT"
    assert victim_key not in r.text
    assert "device_config" not in r.text

    # the victim's key is untouched (no rotation by the foreign token either)
    v = client.post("/cp/devices/verify", headers=b["S"], json={"hardware_uuid": hw, "license_key": victim_key})
    assert v.json()["data"]["valid"] is True
    assert _seats(client, a) == 0


def test_cross_tenant_via_cp_activate_is_rejected(client: TestClient) -> None:
    a, b = _tenant(client), _tenant(client)
    hw = _hw()
    _activate(client, b["token"], hw)
    r = client.post("/cp/devices/activate", headers=a["S"],
                    json={"hardware_uuid": hw, "device_name": "x", "token": a["token"]})
    assert r.status_code == 409
    assert r.json()["errors"][0]["code"] == "DEVICE_BOUND_TO_OTHER_TENANT"


def test_same_tenant_reactivation_rotates_key_without_consuming_seat(client: TestClient) -> None:
    t = _tenant(client)
    hw = _hw()
    first = _activate(client, t["token"], hw).json()["data"]
    old_key = first["license_key"]
    assert _seats(client, t) == 1

    r = _activate(client, t["token"], hw)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["reactivated"] is True
    new_key = data["license_key"]
    assert new_key.startswith("LIC-") and new_key != old_key
    assert data["device"]["license_key"] == new_key
    assert new_key in data["device_config"] and old_key not in data["device_config"]
    assert _seats(client, t) == 1
    assert "device.license_rotate" in _audit_actions(client, hw)
    assert old_key not in str(client.get("/admin/audit", headers=H, params={"limit": 500}).json())


def test_rotated_old_key_stops_verifying_and_heartbeating(client: TestClient) -> None:
    t = _tenant(client)
    hw = _hw()
    old_key = _activate(client, t["token"], hw).json()["data"]["license_key"]
    new_key = _activate(client, t["token"], hw).json()["data"]["license_key"]

    def verify(k: str) -> dict:
        return client.post("/cp/devices/verify", headers=t["S"],
                           json={"hardware_uuid": hw, "license_key": k}).json()["data"]

    assert verify(old_key)["valid"] is False
    assert verify(new_key)["valid"] is True
    hb_old = client.post("/cp/devices/heartbeat", headers=t["S"], json={"hardware_uuid": hw, "license_key": old_key})
    assert hb_old.status_code == 403
    hb_new = client.post("/cp/devices/heartbeat", headers=t["S"], json={"hardware_uuid": hw, "license_key": new_key})
    assert hb_new.status_code == 200


def test_device_config_of_reactivation_is_signed_with_reactivated_flag(client: TestClient) -> None:
    import json

    t = _tenant(client)
    hw = _hw()
    _activate(client, t["token"], hw)
    cfg = json.loads(_activate(client, t["token"], hw).json()["data"]["device_config"])
    assert cfg["payload"]["reactivated"] is True
    assert cfg["payload"]["hardware_uuid"] == hw
    assert cfg["signature"]


# ---- C: throttling -----------------------------------------------------------


def test_failed_activations_lock_out_with_429(client: TestClient) -> None:
    from src import ratelimit

    ratelimit.activation_limiter.max_failures = 3
    for _ in range(3):
        r = _activate(client, "BATCH-NOPE-NOPE-XXXX", _hw())
        assert r.status_code == 403
        assert r.json()["errors"][0]["code"] == "ACTIVATION_TOKEN_INVALID"

    r = _activate(client, "BATCH-NOPE-NOPE-XXXX", _hw())
    assert r.status_code == 429
    assert r.json()["errors"][0]["code"] == "TOO_MANY_ATTEMPTS"
    assert int(r.headers["Retry-After"]) > 0


def test_lockout_also_blocks_a_valid_token_from_the_same_ip(client: TestClient) -> None:
    from src import ratelimit

    t = _tenant(client)
    ratelimit.activation_limiter.max_failures = 2
    for _ in range(2):
        _activate(client, "BATCH-NOPE-NOPE-XXXX", _hw())
    assert _activate(client, t["token"], _hw()).status_code == 429


def test_successful_activations_do_not_count_as_failures(client: TestClient) -> None:
    from src import ratelimit

    t = _tenant(client, seat_limit=10)
    ratelimit.activation_limiter.max_failures = 2
    for _ in range(5):
        assert _activate(client, t["token"], _hw()).status_code == 201


def test_service_token_auth_failures_are_throttled(client: TestClient) -> None:
    from src import ratelimit

    ratelimit.service_auth_limiter.max_failures = 2
    bad = {"Authorization": "Bearer definitely-wrong"}
    for _ in range(2):
        assert client.get("/cp/subscription", headers=bad).status_code == 403
    r = client.get("/cp/subscription", headers=bad)
    assert r.status_code == 429
    assert r.json()["errors"][0]["code"] == "TOO_MANY_ATTEMPTS"
