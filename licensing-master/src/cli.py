"""Operator CLI for admin accounts. Run inside the container:

    docker exec -it <container> python -m src.cli create-admin --email you@example.com
    docker exec -it <container> python -m src.cli --help

Passwords are always prompted (never taken from argv or the environment).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy.engine import Connection

from src import admin_auth
from src.admin_auth import (
    AdminUserError as CliError,
    CreatedAdmin,
    create_admin,
    list_admins,
    reset_password,
    reset_totp,
    revoke_sessions,
)

__all__ = [
    "CliError",
    "CreatedAdmin",
    "create_admin",
    "disable_admin",
    "enable_admin",
    "list_admins",
    "main",
    "reset_password",
    "reset_totp",
    "revoke_sessions",
]


def disable_admin(conn: Connection, email: str) -> None:
    """Deactivate the account and revoke all of its sessions."""
    admin_auth.set_active(conn, email, False)


def enable_admin(conn: Connection, email: str) -> None:
    admin_auth.set_active(conn, email, True)


def _prompt_new_password() -> str:
    first = getpass.getpass("New password (min 12 chars): ")
    if getpass.getpass("Repeat password: ") != first:
        raise CliError("Passwords do not match")
    return first


def _print_qr(uri: str) -> None:
    try:
        import qrcode
    except ImportError:  # the QR is a convenience; the URI/secret are enough
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)


def _print_totp(res: CreatedAdmin) -> None:
    print("\nScan this in your authenticator app (or enter the secret manually):")
    _print_qr(res.otpauth_uri)
    print(f"\nURI:    {res.otpauth_uri}")
    print(f"Secret: {res.totp_secret}")
    print("This secret is shown once; keep it safe.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.cli", description="licensing-master admin accounts")
    sub = p.add_subparsers(dest="command", required=True)
    for name, helptext in (
        ("create-admin", "create an admin (prompts a password, prints the TOTP enrolment)"),
        ("reset-password", "set a new password and revoke the admin's sessions"),
        ("reset-totp", "issue a new TOTP secret and revoke the admin's sessions"),
        ("disable-admin", "deactivate the admin and revoke their sessions"),
        ("enable-admin", "re-activate a disabled admin"),
        ("revoke-sessions", "revoke every live session of the admin"),
    ):
        sub.add_parser(name, help=helptext).add_argument("--email", required=True)
    sub.add_parser("list-admins", help="list admin accounts (no secrets)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from src.db import engine

    try:
        if args.command == "create-admin":
            password = _prompt_new_password()
            with engine().begin() as conn:
                res = create_admin(conn, args.email, password)
            print(f"Created admin {res.email}")
            _print_totp(res)
        elif args.command == "reset-password":
            password = _prompt_new_password()
            with engine().begin() as conn:
                reset_password(conn, args.email, password)
            print("Password changed; existing sessions revoked.")
        elif args.command == "reset-totp":
            with engine().begin() as conn:
                res = reset_totp(conn, args.email)
            print("TOTP reset; existing sessions revoked.")
            _print_totp(res)
        elif args.command == "disable-admin":
            with engine().begin() as conn:
                disable_admin(conn, args.email)
            print("Admin disabled; sessions revoked.")
        elif args.command == "enable-admin":
            with engine().begin() as conn:
                enable_admin(conn, args.email)
            print("Admin enabled.")
        elif args.command == "revoke-sessions":
            with engine().begin() as conn:
                n = revoke_sessions(conn, args.email)
            print(f"Revoked {n} session(s).")
        elif args.command == "list-admins":
            with engine().begin() as conn:
                rows = list_admins(conn)
            for r in rows:
                last = r["last_login_at"].isoformat(sep=" ", timespec="seconds") if r["last_login_at"] else "never"
                state = "active" if r["is_active"] else "DISABLED"
                print(f"{r['email']:<40} {state:<9} last login: {last}")
            if not rows:
                print("No admins yet. Create one with: python -m src.cli create-admin --email ...")
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
