"""Device revocation lifecycle: revoke -> re-activate / reinstate, with correct
seat accounting against both the subscription seat_limit and the batch-token
quota.

Run: LM_DATABASE_URL=postgresql+psycopg://... APP_ENV=test python -m pytest tests -q
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


def _tenant(client: TestClient, *, seat_limit: int = 2, quota: int = 5) -> dict:
    slug = f"t{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/admin/tenants",
        headers=H,
        json={
            "product_code": "farmacia",
            "slug": slug,
            "name": "Lifecycle Co",
            "seat_limit": seat_limit,
            "base_url": "lifecycle.example.com",
        },
    )
    assert r.status_code == 201, r.text
    tid = r.json()["data"]["tenant"]["tenant_id"]
    tok = client.post(
        f"/admin/tenants/{tid}/batch-tokens", headers=H, json={"quota": quota}
    ).json()["data"]
    svc = client.post(
        f"/admin/tenants/{tid}/service-tokens", headers=H, json={"name": "lc"}
    ).json()["data"]["token"]
    return {
        "tid": tid,
        "token": tok["token"],
        "batch_token_id": tok["batch_token_id"],
        "S": {"Authorization": f"Bearer {svc}"},
    }


def _hw() -> str:
    return f"HW-{uuid.uuid4().hex[:12].upper()}"


def _activate(client: TestClient, t: dict, hw: str, token: str | None = None):
    return client.post(
        "/activate",
        json={"hardware_uuid": hw, "device_name": "Caja", "token": token or t["token"]},
    )


def _detail(client: TestClient, tid: int) -> dict:
    return client.get(f"/admin/tenants/{tid}", headers=H).json()["data"]


def _seats_consumed(client: TestClient, t: dict) -> int:
    toks = _detail(client, t["tid"])["batch_tokens"]
    return next(x["seats_consumed"] for x in toks if x["batch_token_id"] == t["batch_token_id"])


def _devices(client: TestClient, tid: int, hw: str) -> list[dict]:
    return [d for d in _detail(client, tid)["devices"] if d["hardware_uuid"] == hw]


def _audit_actions(client: TestClient, hw: str) -> list[str]:
    items = client.get("/admin/audit", headers=H, params={"limit": 500}).json()["data"]["items"]
    return [a["action"] for a in items if str(a["target_id"]) == hw]


def test_revoked_device_can_be_reactivated_with_batch_token(client: TestClient) -> None:
    t = _tenant(client)
    hw = _hw()
    first = _activate(client, t, hw)
    assert first.status_code == 201, first.text
    old_key = first.json()["data"]["license_key"]

    client.post(f"/admin/devices/{hw}/revoke", headers=H)

    again = _activate(client, t, hw)
    assert again.status_code == 201, again.text
    data = again.json()["data"]
    assert data["license_key"].startswith("LIC-")
    assert data["license_key"] != old_key
    assert data["device"]["is_revoked"] is False

    rows = _devices(client, t["tid"], hw)
    assert len(rows) == 1
    assert rows[0]["state"] == "ACTIVE"
    assert _seats_consumed(client, t) == 1


def test_revoke_releases_batch_seat_exactly_once(client: TestClient) -> None:
    t = _tenant(client)
    hw = _hw()
    assert _activate(client, t, hw).status_code == 201
    assert _seats_consumed(client, t) == 1

    r = client.post(f"/admin/devices/{hw}/revoke", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["is_revoked"] is True
    assert _seats_consumed(client, t) == 0

    # second revoke is a no-op: no double decrement, no second audit entry
    other = _hw()
    assert _activate(client, t, other).status_code == 201
    assert _seats_consumed(client, t) == 1
    assert client.post(f"/admin/devices/{hw}/revoke", headers=H).status_code == 200
    assert _seats_consumed(client, t) == 1
    assert _audit_actions(client, hw).count("device.revoke") == 1


def test_revoke_unknown_device_is_404(client: TestClient) -> None:
    assert client.post(f"/admin/devices/{_hw()}/revoke", headers=H).status_code == 404


def test_reinstate_keeps_license_key_and_consumes_seat(client: TestClient) -> None:
    t = _tenant(client)
    hw = _hw()
    key = _activate(client, t, hw).json()["data"]["license_key"]
    client.post(f"/admin/devices/{hw}/revoke", headers=H)
    assert _seats_consumed(client, t) == 0

    r = client.post(f"/admin/devices/{hw}/reinstate", headers=H)
    assert r.status_code == 200, r.text
    dev = r.json()["data"]
    assert dev["is_revoked"] is False
    assert dev["license_key"] == key
    assert _seats_consumed(client, t) == 1

    v = client.post(
        "/cp/devices/verify", headers=t["S"], json={"hardware_uuid": hw, "license_key": key}
    ).json()["data"]
    assert v["valid"] is True and v["state"] == "ACTIVE"
    assert "device.reinstate" in _audit_actions(client, hw)


def test_reinstate_not_revoked_is_noop(client: TestClient) -> None:
    t = _tenant(client)
    hw = _hw()
    _activate(client, t, hw)
    r = client.post(f"/admin/devices/{hw}/reinstate", headers=H)
    assert r.status_code == 200, r.text
    assert _seats_consumed(client, t) == 1
    assert "device.reinstate" not in _audit_actions(client, hw)


def test_reinstate_unknown_device_is_404(client: TestClient) -> None:
    assert client.post(f"/admin/devices/{_hw()}/reinstate", headers=H).status_code == 404


def test_reinstate_and_reactivate_respect_seat_limit(client: TestClient) -> None:
    t = _tenant(client, seat_limit=1)
    hw = _hw()
    _activate(client, t, hw)
    client.post(f"/admin/devices/{hw}/revoke", headers=H)
    assert _activate(client, t, _hw()).status_code == 201  # takes the only seat

    r = client.post(f"/admin/devices/{hw}/reinstate", headers=H)
    assert r.status_code == 409 and r.json()["errors"][0]["code"] == "SEAT_LIMIT_REACHED"
    assert _devices(client, t["tid"], hw)[0]["is_revoked"] is True

    r = _activate(client, t, hw)
    assert r.status_code == 409 and r.json()["errors"][0]["code"] == "SEAT_LIMIT_REACHED"


def test_reinstate_and_reactivate_respect_batch_quota(client: TestClient) -> None:
    t = _tenant(client, seat_limit=5, quota=1)
    hw = _hw()
    _activate(client, t, hw)
    client.post(f"/admin/devices/{hw}/revoke", headers=H)
    # use the freed seat on a different machine via /cp/seats/consume
    assert client.post(
        "/cp/seats/consume", headers=t["S"], json={"token": t["token"], "hardware_uuid": _hw()}
    ).status_code == 201

    r = client.post(f"/admin/devices/{hw}/reinstate", headers=H)
    assert r.status_code == 409 and r.json()["errors"][0]["code"] == "SEAT_LIMIT_REACHED"
    assert _seats_consumed(client, t) == 1


def test_cp_consume_seat_rebooks_revoked_row(client: TestClient) -> None:
    """Regression: cp_consume_seat keeps re-booking the revoked row."""
    t = _tenant(client)
    hw = _hw()
    body = {"token": t["token"], "hardware_uuid": hw, "device_name": "Caja Y"}
    assert client.post("/cp/seats/consume", headers=t["S"], json=body).json()["data"]["consumed"] is True
    client.post(f"/admin/devices/{hw}/revoke", headers=H)
    assert _seats_consumed(client, t) == 0

    r = client.post("/cp/seats/consume", headers=t["S"], json=body)
    assert r.status_code == 201, r.text
    assert r.json()["data"] == {"consumed": True, "reactivated": False, "hardware_uuid": hw}
    assert len(_devices(client, t["tid"], hw)) == 1
    assert _seats_consumed(client, t) == 1
