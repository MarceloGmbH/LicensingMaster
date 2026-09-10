"""End-to-end smoke of licensing-master against a live sistema_farmacia_licensing DB.

Run: LM_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/sistema_farmacia_licensing \
     APP_ENV=test python -m pytest apps/licensing-master/tests -q
"""

from __future__ import annotations

import os
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


def test_full_lifecycle(client: TestClient) -> None:
    slug = f"t{uuid.uuid4().hex[:8]}"

    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/admin/me", headers=H).json()["data"]["email"] == "tester@alanadev.com"
    assert "farmacia" in [p["code"] for p in client.get("/admin/products", headers=H).json()["data"]["items"]]

    r = client.post(
        "/admin/tenants",
        headers=H,
        json={"product_code": "farmacia", "slug": slug, "name": "Smoke Co", "seat_limit": 2},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["data"]["tenant"]["tenant_id"]

    # set the tenant API URL (injected into device_config on central activation)
    patched = client.patch(
        f"/admin/tenants/{tid}", headers=H, json={"base_url": "smoke.example.com/api/v1"}
    ).json()["data"]
    assert patched["tenant"]["base_url"] == "https://smoke.example.com"

    tok = client.post(
        f"/admin/tenants/{tid}/batch-tokens", headers=H, json={"quota": 5}
    ).json()["data"]["token"]
    svc = client.post(
        f"/admin/tenants/{tid}/service-tokens", headers=H, json={"name": "smoke"}
    ).json()["data"]["token"]
    S = {"Authorization": f"Bearer {svc}"}

    assert client.get("/cp/subscription", headers=S).json()["data"]["is_paid"] is True
    assert client.post(
        "/cp/activation-tokens/verify", headers=S, json={"token": tok}
    ).json()["data"]["valid"] is True

    hw = f"HW-{uuid.uuid4().hex[:12].upper()}"
    r = client.post(
        "/cp/devices/activate",
        headers=S,
        json={"hardware_uuid": hw, "device_name": "Caja X", "token": tok},
    )
    assert r.status_code == 201, r.text
    lk = r.json()["data"]["license_key"]
    assert lk.startswith("LIC-")

    # public first-run activation (no auth) returns the tenant URL
    hw_pub = f"HW-{uuid.uuid4().hex[:12].upper()}"
    pub = client.post(
        "/activate",
        json={"hardware_uuid": hw_pub, "device_name": "Caja Pub", "token": tok},
    )
    assert pub.status_code == 201, pub.text
    pd = pub.json()["data"]
    assert pd["tenant_base_url"] == "https://smoke.example.com"
    assert pd["license_key"].startswith("LIC-")
    client.post(f"/admin/devices/{hw_pub}/revoke", headers=H)  # free that seat for the rest

    st = client.get("/cp/devices/status", headers=S, params={"hardware_uuid": hw}).json()["data"]
    assert st["state"] == "ACTIVE" and st["days_remaining"] >= 0

    # device-license gate (ADR-0015): the right key passes, a wrong one fails
    v = client.post(
        "/cp/devices/verify", headers=S, json={"hardware_uuid": hw, "license_key": lk}
    ).json()["data"]
    assert v["valid"] is True and v["state"] == "ACTIVE"
    v_bad = client.post(
        "/cp/devices/verify", headers=S, json={"hardware_uuid": hw, "license_key": "LIC-wrong"}
    ).json()["data"]
    assert v_bad["valid"] is False

    hb = client.post(
        "/cp/devices/heartbeat", headers=S, json={"hardware_uuid": hw, "license_key": lk}
    ).json()["data"]
    assert hb["last_heartbeat_at"] is not None

    # a product backend that manages its own license key books a seat via
    # /cp/seats/consume (no device_config minted here).
    hw2 = f"HW-{uuid.uuid4().hex[:12].upper()}"
    r = client.post(
        "/cp/seats/consume",
        headers=S,
        json={"token": tok, "hardware_uuid": hw2, "device_name": "Caja Y"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["consumed"] is True
    # idempotent — same hardware, no new seat
    assert (
        client.post(
            "/cp/seats/consume", headers=S, json={"token": tok, "hardware_uuid": hw2}
        ).json()["data"]["consumed"]
        is False
    )
    # 3rd distinct seat exceeds seat_limit=2
    assert (
        client.post(
            "/cp/seats/consume",
            headers=S,
            json={"token": tok, "hardware_uuid": f"HW-{uuid.uuid4().hex[:12].upper()}"},
        ).status_code
        == 409
    )
    # release hands the seat back
    assert client.post(
        "/cp/seats/release", headers=S, json={"hardware_uuid": hw2}
    ).json()["data"]["released"] is True

    detail = client.get(f"/admin/tenants/{tid}", headers=H).json()["data"]
    assert detail["subscription"]["seats_used"] == 1

    before = detail["subscription"]["valid_until"]
    after = client.post(
        f"/admin/tenants/{tid}/payments", headers=H, json={"amount": "100"}
    ).json()["data"]["subscription"]["valid_until"]
    assert after > before

    # admin revoke frees the seat
    client.post(f"/admin/devices/{hw}/revoke", headers=H)
    assert (
        client.get(f"/admin/tenants/{tid}", headers=H).json()["data"]["subscription"]["seats_used"]
        == 0
    )
