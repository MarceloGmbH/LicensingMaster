"""licensing-master configuration (ADR-0013). Env-driven; safe local defaults."""

from __future__ import annotations

import base64
import binascii

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_ed25519_private_key(key_b64: str):
    """Decode a base64 raw 32-byte Ed25519 private key; ValueError if unusable."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        raw = base64.b64decode(key_b64.strip(), validate=True)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "LM_ED25519_PRIVATE_KEY_B64 must be base64 of a raw 32-byte Ed25519 private key"
        ) from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "licensing-master"
    APP_ENV: str = "development"
    VERSION: str = "1.0.0"
    APP_DEBUG: bool = True

    # The one control-plane database (ADR-0013). Full URL wins; otherwise assembled.
    LM_DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5433/sistema_farmacia_licensing"
    )

    # Ed25519 private key (base64, 32 raw bytes) used to sign device_config.dat.
    # REQUIRED outside development/test (startup fails without a valid key).
    # In dev/test only, empty -> a deterministic DEVCFG:<sha256> digest is used.
    LM_ED25519_PRIVATE_KEY_B64: str = ""

    # Native admin login (password + TOTP). Sessions are server-side: idle
    # timeout slides on every request, the absolute lifetime never extends.
    LM_ADMIN_SESSION_IDLE_SECONDS: int = Field(default=30 * 60, ge=60)
    LM_ADMIN_SESSION_ABSOLUTE_SECONDS: int = Field(default=8 * 3600, ge=60)

    # Subscription defaults for a freshly-created tenant.
    LM_DEFAULT_WINDOW_DAYS: int = 30
    LM_DEFAULT_GRACE_DAYS: int = 5
    LM_DEFAULT_SEAT_LIMIT: int = 5

    CORS_ORIGINS: list[str] = ["*"]

    # Brute-force throttling (in-memory, per client IP, single process).
    # Public POST /activate: FAILED token attempts only.
    LM_ACTIVATE_MAX_FAILURES: int = Field(default=10, ge=1)
    LM_ACTIVATE_FAILURE_WINDOW_SECONDS: int = Field(default=600, ge=1)
    LM_ACTIVATE_LOCKOUT_SECONDS: int = Field(default=900, ge=1)
    # /cp/* service-token authentication failures.
    LM_CP_AUTH_MAX_FAILURES: int = Field(default=20, ge=1)
    LM_CP_AUTH_FAILURE_WINDOW_SECONDS: int = Field(default=600, ge=1)
    LM_CP_AUTH_LOCKOUT_SECONDS: int = Field(default=900, ge=1)

    # Admin login failures (and wrong re-entered passwords on tenant deletion):
    # counted per typed e-mail and, separately, per client IP.
    LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES: int = Field(default=5, ge=1)
    LM_ADMIN_LOGIN_EMAIL_WINDOW_SECONDS: int = Field(default=900, ge=1)
    LM_ADMIN_LOGIN_EMAIL_LOCKOUT_SECONDS: int = Field(default=900, ge=1)
    LM_ADMIN_LOGIN_IP_MAX_FAILURES: int = Field(default=10, ge=1)
    LM_ADMIN_LOGIN_IP_WINDOW_SECONDS: int = Field(default=900, ge=1)
    LM_ADMIN_LOGIN_IP_LOCKOUT_SECONDS: int = Field(default=900, ge=1)

    @model_validator(mode="after")
    def _signing_key_required_outside_dev(self) -> "Settings":
        if self.LM_ED25519_PRIVATE_KEY_B64.strip():
            parse_ed25519_private_key(self.LM_ED25519_PRIVATE_KEY_B64)  # invalid -> startup error
        elif not self.is_dev:
            raise ValueError(
                "LM_ED25519_PRIVATE_KEY_B64 is required when APP_ENV is not development/test "
                "(see scripts/gen_ed25519_keypair.py)"
            )
        return self

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV.strip().lower() in {"development", "dev", "local", "test", "testing", "ci"}


settings = Settings()
