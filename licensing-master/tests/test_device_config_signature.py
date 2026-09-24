"""Regression test: the `payload` substring embedded in `device_config` must
be byte-identical to what `sign_payload()` actually signed.

Bug: `_device_config()` used to re-serialize `payload` without
`sort_keys=True` when embedding it in the outer `{"payload": ..., "signature":
...}` envelope, while `sign_payload()` signs the canonical (sorted) form —
see signer.py's `_canonical()`. A client extracting the raw "payload"
substring from the embedded JSON (the only byte-correct way to verify,
documented in docs/INTEGRATION.md — JSON round-tripping via parse/stringify
is not guaranteed to reproduce the exact signed bytes) was therefore always
verifying different bytes than what was signed. Ed25519 verification failed
for every real device activation, unconditionally. No existing test caught
this: test_smoke.py checks `tenant_base_url`/`license_key` on the activation
response but never parses or verifies `device_config`'s signature.

Pure function-level test — no DB, no network.
"""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.config import settings
from src.service import _device_config


def _extract_raw_payload_substring(device_config_json: str) -> str:
    """Same byte-extraction approach a real client must use (see
    docs/INTEGRATION.md): find the raw "payload" object substring by
    balanced-brace scanning, never JSON.parse -> re-stringify."""
    key = '"payload":'
    start = device_config_json.index(key) + len(key)
    depth = 0
    for i in range(start, len(device_config_json)):
        ch = device_config_json[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return device_config_json[start : i + 1]
    raise AssertionError("unbalanced braces while extracting payload")


@pytest.fixture()
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real, throwaway Ed25519 key — never used outside this test."""
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes_raw()
    monkeypatch.setattr(settings, "LM_ED25519_PRIVATE_KEY_B64", base64.b64encode(raw).decode())


def test_embedded_payload_bytes_match_what_was_actually_signed(signing_key: None) -> None:
    # Deliberately NOT alphabetically ordered by insertion — this is exactly
    # the shape _activation_result() constructs (schema, product_code,
    # tenant_slug, ... — not sorted), which is what exposed the bug: a
    # payload whose insertion order already happened to be sorted would have
    # masked it.
    payload = {
        "schema": "device_config.dat/v1",
        "product_code": None,
        "tenant_slug": "demo-pos",
        "tenant_base_url": "https://demo-pos.alanadev.com",
        "device_name": "Terminal 1",
        "hardware_uuid": "hw-test",
        "license_key": "LIC-test",
        "valid_from": "2026-09-24T00:00:00Z",
        "valid_until": "2026-10-24T00:00:00Z",
        "issued_at": "2026-09-24T00:00:00Z",
    }

    device_config_json = _device_config(payload)
    parsed = json.loads(device_config_json)
    assert parsed["signature"].startswith("ed25519:")

    signed_canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    embedded_raw = _extract_raw_payload_substring(device_config_json)

    assert embedded_raw == signed_canonical, (
        "The payload bytes embedded in device_config must be byte-identical "
        "to the canonical form sign_payload() actually signed, or every "
        "real client-side Ed25519 verification fails."
    )
