"""device_config.dat signer.

Prod: Ed25519 over the canonical JSON with the tenant/master private key
(`LM_ED25519_PRIVATE_KEY_B64`). Dev fallback: a deterministic `DEVCFG:<sha256>`
digest so activation works without a key configured.
"""

from __future__ import annotations

import base64
import hashlib
import json

from src.config import settings

_PREFIX = "DEVCFG:"


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict) -> str:
    canonical = _canonical(payload)
    key_b64 = settings.LM_ED25519_PRIVATE_KEY_B64.strip()
    if not key_b64:
        return f"{_PREFIX}{hashlib.sha256(canonical).hexdigest()[:32]}"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        raw = base64.b64decode(key_b64)
        key = Ed25519PrivateKey.from_private_bytes(raw)
        return "ed25519:" + base64.b64encode(key.sign(canonical)).decode("ascii")
    except Exception:  # noqa: BLE001 - fall back rather than fail activation
        return f"{_PREFIX}{hashlib.sha256(canonical).hexdigest()[:32]}"
