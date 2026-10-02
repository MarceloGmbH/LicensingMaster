"""device_config.dat signer.

Ed25519 over the canonical JSON with the master private key
(`LM_ED25519_PRIVATE_KEY_B64`). Fails closed: outside development/test a
missing or invalid key raises `SigningKeyError` (and `Settings` refuses to
start). The unkeyed `DEVCFG:<sha256>` digest exists ONLY as a dev/test
convenience and is never used when a key is configured or in production.
"""

from __future__ import annotations

import base64
import hashlib
import json

from src.config import parse_ed25519_private_key, settings

_PREFIX = "DEVCFG:"


class SigningKeyError(RuntimeError):
    """The device_config signing key is missing or invalid."""


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict) -> str:
    canonical = _canonical(payload)
    key_b64 = settings.LM_ED25519_PRIVATE_KEY_B64.strip()
    if not key_b64:
        if settings.is_dev:
            return f"{_PREFIX}{hashlib.sha256(canonical).hexdigest()[:32]}"
        raise SigningKeyError("LM_ED25519_PRIVATE_KEY_B64 is not configured")
    try:
        key = parse_ed25519_private_key(key_b64)
    except ValueError as exc:
        raise SigningKeyError(str(exc)) from exc
    return "ed25519:" + base64.b64encode(key.sign(canonical)).decode("ascii")
