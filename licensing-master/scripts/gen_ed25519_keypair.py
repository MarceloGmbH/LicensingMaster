"""Generate the Ed25519 keypair used to sign/verify device_config.dat.

    python scripts/gen_ed25519_keypair.py

Prints two base64 values (raw 32 bytes each):
  * LM_ED25519_PRIVATE_KEY_B64  -> secret; set it ONLY on licensing-master.
  * VITE_DEVICE_CONFIG_PUBKEY   -> public; baked into the desktop build.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    key = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(key.private_bytes_raw()).decode()
    public_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    print(f"LM_ED25519_PRIVATE_KEY_B64={private_b64}")
    print(f"VITE_DEVICE_CONFIG_PUBKEY={public_b64}")


if __name__ == "__main__":
    main()
