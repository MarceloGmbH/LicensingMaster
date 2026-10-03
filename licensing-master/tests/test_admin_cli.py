"""Function-level tests for the admin CLI (`python -m src.cli`)."""

from __future__ import annotations

import os
import uuid

import pyotp
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.skipif(
    not os.getenv("LM_DATABASE_URL"), reason="LM_DATABASE_URL not set"
)

PW = "a perfectly fine password"


def _email() -> str:
    return f"cli-{uuid.uuid4().hex[:8]}@alanadev.com"


@pytest.fixture()
def conn():
    from src.db import engine

    with engine().begin() as c:
        yield c


def _user(conn, email):
    from src.tables import admin_users

    return conn.execute(select(admin_users).where(admin_users.c.email == email)).mappings().first()


def _sessions(conn, email, *, only_live=False):
    from src.tables import admin_sessions, admin_users

    q = (
        select(admin_sessions)
        .join(admin_users, admin_users.c.admin_user_id == admin_sessions.c.admin_user_id)
        .where(admin_users.c.email == email)
    )
    if only_live:
        q = q.where(admin_sessions.c.revoked_at.is_(None))
    return conn.execute(q).mappings().all()


def test_create_admin_hashes_password_and_returns_totp_material(conn) -> None:
    from src import cli

    email = _email().upper()
    res = cli.create_admin(conn, email, PW)
    row = _user(conn, email.lower())
    assert row["email"] == email.lower() and row["is_active"] is True
    assert row["password_hash"].startswith("$argon2id$") and PW not in row["password_hash"]
    assert row["totp_secret"] == res.totp_secret
    assert pyotp.TOTP(res.totp_secret).verify(pyotp.TOTP(res.totp_secret).now())
    assert res.otpauth_uri.startswith("otpauth://totp/") and res.totp_secret in res.otpauth_uri
    assert "Licensing" in res.otpauth_uri


def test_create_admin_enforces_min_password_length(conn) -> None:
    from src import cli

    with pytest.raises(cli.CliError, match="12"):
        cli.create_admin(conn, _email(), "short-pw")
    with pytest.raises(cli.CliError):
        cli.create_admin(conn, "not-an-email", PW)


def test_create_admin_rejects_duplicates(conn) -> None:
    from src import cli

    email = _email()
    cli.create_admin(conn, email, PW)
    with pytest.raises(cli.CliError, match="exists"):
        cli.create_admin(conn, email.upper(), PW)


def test_reset_password_changes_hash_and_revokes_sessions(conn) -> None:
    from src import admin_auth, cli

    email = _email()
    cli.create_admin(conn, email, PW)
    user = _user(conn, email)
    admin_auth.create_session(conn, user["admin_user_id"], ip="1.1.1.1", user_agent="t")
    old = user["password_hash"]
    cli.reset_password(conn, email, "a brand new password!")
    assert _user(conn, email)["password_hash"] != old
    assert admin_auth.verify_password(_user(conn, email)["password_hash"], "a brand new password!")
    assert _sessions(conn, email, only_live=True) == []
    with pytest.raises(cli.CliError, match="12"):
        cli.reset_password(conn, email, "short")


def test_reset_totp_issues_a_new_secret_and_revokes_sessions(conn) -> None:
    from src import admin_auth, cli

    email = _email()
    created = cli.create_admin(conn, email, PW)
    user = _user(conn, email)
    admin_auth.create_session(conn, user["admin_user_id"], ip=None, user_agent=None)
    res = cli.reset_totp(conn, email)
    row = _user(conn, email)
    assert res.totp_secret != created.totp_secret and row["totp_secret"] == res.totp_secret
    assert row["totp_last_step"] is None
    assert _sessions(conn, email, only_live=True) == []


def test_disable_and_enable_admin(conn) -> None:
    from src import admin_auth, cli

    email = _email()
    cli.create_admin(conn, email, PW)
    admin_auth.create_session(conn, _user(conn, email)["admin_user_id"], ip=None, user_agent=None)
    cli.disable_admin(conn, email)
    assert _user(conn, email)["is_active"] is False
    assert _sessions(conn, email, only_live=True) == []
    cli.enable_admin(conn, email)
    assert _user(conn, email)["is_active"] is True


def test_revoke_sessions_returns_count(conn) -> None:
    from src import admin_auth, cli

    email = _email()
    cli.create_admin(conn, email, PW)
    uid = _user(conn, email)["admin_user_id"]
    for _ in range(3):
        admin_auth.create_session(conn, uid, ip=None, user_agent=None)
    assert cli.revoke_sessions(conn, email) == 3
    assert cli.revoke_sessions(conn, email) == 0


def test_list_admins_never_exposes_secrets(conn) -> None:
    from src import cli

    email = _email()
    cli.create_admin(conn, email, PW)
    rows = cli.list_admins(conn)
    mine = next(r for r in rows if r["email"] == email)
    assert mine["is_active"] is True and "password_hash" not in mine and "totp_secret" not in mine


def test_unknown_email_is_an_error(conn) -> None:
    from src import cli

    for fn in (cli.reset_totp, cli.disable_admin, cli.enable_admin, cli.revoke_sessions):
        with pytest.raises(cli.CliError, match="not found"):
            fn(conn, "ghost@alanadev.com")
    with pytest.raises(cli.CliError, match="not found"):
        cli.reset_password(conn, "ghost@alanadev.com", PW)


def test_cli_actions_are_audited(conn) -> None:
    from src import cli
    from src.tables import audit_log

    email = _email()
    cli.create_admin(conn, email, PW)
    cli.disable_admin(conn, email)
    actions = {
        r["action"]
        for r in conn.execute(select(audit_log).where(audit_log.c.target_id == email)).mappings()
    }
    assert {"admin.create", "admin.disable"} <= actions


def test_main_dispatch_with_prompts(monkeypatch, capsys) -> None:
    from src import cli
    from src.db import engine

    email = _email()
    answers = iter([PW, PW])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["create-admin", "--email", email]) == 0
    out = capsys.readouterr().out
    assert "otpauth://totp/" in out and PW not in out
    with engine().begin() as c:
        assert _user(c, email) is not None

    answers = iter([PW, "different password!!"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["reset-password", "--email", email]) == 1  # mismatch
    assert cli.main(["list-admins"]) == 0
    assert email in capsys.readouterr().out
