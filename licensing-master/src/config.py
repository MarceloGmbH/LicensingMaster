"""licensing-master configuration (ADR-0013). Env-driven; safe local defaults."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Empty -> a deterministic dev signer (DEVCFG:<sha256>) is used instead.
    LM_ED25519_PRIVATE_KEY_B64: str = ""

    # Cloudflare Access (human auth for /admin/*). The portal sits behind a
    # Cloudflare Tunnel + Access application; this service verifies the JWT it
    # injects. Leave LM_ACCESS_TEAM_DOMAIN empty in dev to accept a plain
    # `X-Dev-Admin-Email` header instead.
    LM_ACCESS_TEAM_DOMAIN: str = ""  # e.g. "alanadev.cloudflareaccess.com"
    LM_ACCESS_AUD: str = ""  # the Access application's AUD tag
    LM_ADMIN_EMAILS: str = ""  # comma-separated allow-list; empty = any verified email

    # Subscription defaults for a freshly-created tenant.
    LM_DEFAULT_WINDOW_DAYS: int = 30
    LM_DEFAULT_GRACE_DAYS: int = 5
    LM_DEFAULT_SEAT_LIMIT: int = 5

    CORS_ORIGINS: list[str] = ["*"]

    @property
    def admin_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.LM_ADMIN_EMAILS.split(",") if e.strip()}

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV.strip().lower() in {"development", "dev", "local", "test", "testing", "ci"}


settings = Settings()
