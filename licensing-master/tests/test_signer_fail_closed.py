"""The signer must fail closed outside development (audit A3)."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from src import signer
from src.config import Settings, settings


def _key_b64() -> str:
    return base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()


def test_dev_without_key_uses_devcfg_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "test")
    monkeypatch.setattr(settings, "LM_ED25519_PRIVATE_KEY_B64", "")
    assert signer.sign_payload({"a": 1}).startswith("DEVCFG:")


def test_production_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "LM_ED25519_PRIVATE_KEY_B64", "")
    with pytest.raises(signer.SigningKeyError):
        signer.sign_payload({"a": 1})


@pytest.mark.parametrize("env", ["test", "production"])
def test_invalid_key_raises_in_every_env(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setattr(settings, "APP_ENV", env)
    monkeypatch.setattr(settings, "LM_ED25519_PRIVATE_KEY_B64", "not-base64-!!!")
    with pytest.raises(signer.SigningKeyError):
        signer.sign_payload({"a": 1})
    monkeypatch.setattr(settings, "LM_ED25519_PRIVATE_KEY_B64", base64.b64encode(b"short").decode())
    with pytest.raises(signer.SigningKeyError):
        signer.sign_payload({"a": 1})


def test_valid_key_signs_with_ed25519(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "LM_ED25519_PRIVATE_KEY_B64", _key_b64())
    assert signer.sign_payload({"a": 1}).startswith("ed25519:")


def test_settings_reject_production_without_key() -> None:
    with pytest.raises(ValidationError):
        Settings(APP_ENV="production", LM_ED25519_PRIVATE_KEY_B64="")


def test_settings_reject_production_with_invalid_key() -> None:
    with pytest.raises(ValidationError):
        Settings(APP_ENV="production", LM_ED25519_PRIVATE_KEY_B64="garbage")


def test_settings_accept_production_with_valid_key() -> None:
    s = Settings(APP_ENV="production", LM_ED25519_PRIVATE_KEY_B64=_key_b64())
    assert not s.is_dev


def test_settings_accept_dev_without_key() -> None:
    assert Settings(APP_ENV="development", LM_ED25519_PRIVATE_KEY_B64="").is_dev


def test_tenant_base_url_requires_https_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import service
    from src.errors import ValidationError as AppValidationError

    monkeypatch.setattr(settings, "APP_ENV", "production")
    with pytest.raises(AppValidationError):
        service._norm_base_url("http://tenant.example.com")
    assert service._norm_base_url("tenant.example.com") == "https://tenant.example.com"
    assert service._norm_base_url("https://tenant.example.com/api/v1") == "https://tenant.example.com"

    monkeypatch.setattr(settings, "APP_ENV", "development")
    assert service._norm_base_url("http://localhost:8000") == "http://localhost:8000"
