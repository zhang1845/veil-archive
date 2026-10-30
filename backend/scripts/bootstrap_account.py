"""Create one new account in an existing schema; never reset or promote users.

Run explicitly from the deployed backend directory. Passwords are accepted only
through getpass or an EOF-terminated stdin pipe, never a command-line argument.
Importing this module does not load .env, connect to a database, or create tables.
"""
from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import re
import sys
from typing import cast, NoReturn

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


class BootstrapError(ValueError):
    """Fixed, secret-free operator errors."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse normally echoes unknown argument values, including an
        # accidentally supplied --password. Keep all usage errors value-free.
        self.exit(2, "BOOTSTRAP_ARGUMENTS_INVALID: use --help for accepted options.\n")


def validate_inputs(username: str, email: str, password: str, role: str) -> str:
    from email_validator import EmailNotValidError, validate_email

    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,50}", username):
        raise BootstrapError("BOOTSTRAP_USERNAME_INVALID")
    if role not in {"admin", "player"}:
        raise BootstrapError("BOOTSTRAP_ROLE_INVALID")
    if (not isinstance(password, str) or not 12 <= len(password) <= 100
            or any(ord(char) < 32 for char in password)):
        raise BootstrapError("BOOTSTRAP_PASSWORD_INVALID")
    try:
        normalized = validate_email(email, check_deliverability=False).normalized
    except (EmailNotValidError, TypeError):
        raise BootstrapError("BOOTSTRAP_EMAIL_INVALID") from None
    if len(normalized) > 100:
        raise BootstrapError("BOOTSTRAP_EMAIL_INVALID")
    return normalized


def create_account(db, *, username: str, email: str, password: str, role: str = "player") -> int:
    """Append one user in the caller's transaction; default to an ordinary player."""
    from sqlalchemy import or_
    from sqlalchemy.exc import IntegrityError
    from src.db.models import User
    from src.services.auth_service import AuthService

    email = validate_inputs(username, email, password, role)
    if db.query(User.id).filter(or_(User.username == username, User.email == email)).first() is not None:
        raise BootstrapError("BOOTSTRAP_ACCOUNT_EXISTS")
    account = User(username=username, email=email, nickname=username,
                   hashed_password=AuthService.get_password_hash(password),
                   is_admin=role == "admin", is_active=True, is_verified=False)
    db.add(account)
    try:
        db.flush()
    except IntegrityError:
        raise BootstrapError("BOOTSTRAP_ACCOUNT_EXISTS") from None
    return cast(int, account.id)


def read_password(*, password_stdin: bool) -> str:
    if password_stdin:
        if sys.stdin.isatty():
            raise BootstrapError("BOOTSTRAP_PASSWORD_STDIN_REQUIRES_PIPE")
        value = sys.stdin.readline(1025)
        if len(value) > 1024 or sys.stdin.read(1):
            raise BootstrapError("BOOTSTRAP_PASSWORD_INPUT_INVALID")
        return value.removesuffix("\n").removesuffix("\r")
    password = getpass.getpass("New password (at least 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise BootstrapError("BOOTSTRAP_PASSWORD_CONFIRMATION_MISMATCH")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description="Create a new account only; existing accounts are never changed.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", choices=("admin", "player"), required=True)
    parser.add_argument("--password-stdin", action="store_true", help="Read one password line from a pipe instead of getpass.")
    args = parser.parse_args(argv)
    manager = None
    try:
        password = read_password(password_stdin=args.password_stdin)
        validate_inputs(args.username, args.email, password, args.role)
        from src.core.environment import load_project_environment
        load_project_environment()
        from sqlalchemy import inspect
        from src.core.security_preflight import enforce_security_configuration
        from src.db.session import db_manager
        enforce_security_configuration(reporter=lambda _: None)
        manager = db_manager
        manager.initialize()
        if not inspect(manager.engine).has_table("users"):
            raise BootstrapError("BOOTSTRAP_SCHEMA_NOT_READY")
        with manager.session_scope() as db:
            identifier = create_account(db, username=args.username, email=args.email,
                                        password=password, role=args.role)
        print(f"Account created: id={identifier}, role={args.role}. Password was not printed.")
        return 0
    except BootstrapError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("BOOTSTRAP_CANCELLED", file=sys.stderr)
        return 130
    except Exception:
        print("BOOTSTRAP_FAILED: check the configured database and existing schema; no credentials were printed.", file=sys.stderr)
        return 1
    finally:
        if manager is not None:
            manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
