"""Shared fixtures.

* The failure limiters are process-wide singletons, so reset them around every
  test to keep tests independent.
* ``client`` is a TestClient already authenticated as an admin through the real
  login flow (password + TOTP -> ``lm_session`` cookie), sending the CSRF header
  the portal sends. ``anon_client`` has the header but no session.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import pyotp
import pytest
from fastapi.testclient import TestClient

CSRF = {"X-Requested-With": "lm-portal"}
ADMIN_EMAIL = "tester@alanadev.com"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _reset_limiters():
    from src import ratelimit

    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@dataclass
class AdminAccount:
    email: str
    password: str
    secret: str

    def code(self, steps: int = 0) -> str:
        """Current TOTP code, shifted by whole 30 s steps (use +1 for a second
        login in the same window: the same step can only be used once)."""
        return pyotp.TOTP(self.secret).at(time.time() + 30 * steps)


def make_admin(email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> AdminAccount:
    """(Re)create an admin straight in the DB, dropping any previous row (and its sessions)."""
    from sqlalchemy import delete

    from src import admin_auth
    from src.db import engine
    from src.tables import admin_users

    with engine().begin() as conn:
        conn.execute(delete(admin_users).where(admin_users.c.email == email.lower()))
        created = admin_auth.create_admin(conn, email, password)
    return AdminAccount(email=email.lower(), password=password, secret=created.totp_secret)


@pytest.fixture()
def admin() -> AdminAccount:
    if not os.getenv("LM_DATABASE_URL"):
        pytest.skip("LM_DATABASE_URL not set")
    return make_admin()


@pytest.fixture()
def anon_client() -> TestClient:
    from src.main import app

    return TestClient(app, headers=CSRF)


@pytest.fixture()
def client(anon_client: TestClient, admin: AdminAccount) -> TestClient:
    r = anon_client.post(
        "/admin/auth/login",
        json={"email": admin.email, "password": admin.password, "totp_code": admin.code()},
    )
    assert r.status_code == 200, r.text
    return anon_client
