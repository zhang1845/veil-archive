"""Offline account provisioning; never use the user's database or .env."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.base import SQLAlchemyBase
from src.db.models import User
from src.services.auth_service import AuthService

spec = spec_from_file_location("safe_account_bootstrap", Path(__file__).resolve().parents[2] / "scripts/bootstrap_account.py")
assert spec and spec.loader
bootstrap = module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def accounts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounts.sqlite'}")
    SQLAlchemyBase.metadata.create_all(engine)
    factory = sessionmaker(engine)
    yield factory
    engine.dispose()


def test_bootstrap_defaults_to_player_and_stores_only_a_password_hash(accounts):
    with accounts.begin() as db:
        identifier = bootstrap.create_account(db, username="player_one", email="one@example.com", password="synthetic-password-one")
    with accounts() as db:
        account = db.get(User, identifier)
        assert account.is_admin is False and account.is_active is True
        assert account.hashed_password != "synthetic-password-one"
        assert AuthService.verify_password("synthetic-password-one", account.hashed_password)


def test_bootstrap_admin_role_is_explicit_for_a_new_account(accounts):
    with accounts.begin() as db:
        identifier = bootstrap.create_account(db, username="admin_one", email="admin@example.com", password="synthetic-admin-password", role="admin")
    with accounts() as db:
        assert db.get(User, identifier).is_admin is True


@pytest.mark.parametrize("name,email", [("player_one", "different@example.com"), ("different", "one@example.com")])
def test_bootstrap_existing_accounts_are_never_reset_or_promoted(accounts, name, email):
    with accounts.begin() as db:
        identifier = bootstrap.create_account(db, username="player_one", email="one@example.com", password="synthetic-original-password")
    with accounts.begin() as db:
        with pytest.raises(bootstrap.BootstrapError, match="BOOTSTRAP_ACCOUNT_EXISTS"):
            bootstrap.create_account(db, username=name, email=email, password="synthetic-replacement-password", role="admin")
    with accounts() as db:
        account = db.get(User, identifier)
        assert account.is_admin is False
        assert AuthService.verify_password("synthetic-original-password", account.hashed_password)
        assert db.query(User).count() == 1


@pytest.mark.parametrize("patch", [{"role": "owner"}, {"password": "short"}, {"username": "bad/@_name"}, {"email": "not-an-email"}])
def test_bootstrap_bad_inputs_are_refused_before_writing(accounts, patch):
    values = dict(username="player_one", email="one@example.com", password="synthetic-player-password", role="player") | patch
    with accounts.begin() as db:
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.create_account(db, **values)
        assert db.query(User).count() == 0


def test_bootstrap_stdin_password_requires_one_line_and_does_not_echo(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap.sys, "stdin", io.StringIO("synthetic-stdin-password\n"))
    assert bootstrap.read_password(password_stdin=True) == "synthetic-stdin-password"
    assert capsys.readouterr() == ("", "")
    monkeypatch.setattr(bootstrap.sys, "stdin", io.StringIO("first\nsecond\n"))
    with pytest.raises(bootstrap.BootstrapError, match="INPUT_INVALID"):
        bootstrap.read_password(password_stdin=True)


def test_bootstrap_cli_requires_role_and_has_no_plaintext_password_argument(capsys):
    with pytest.raises(SystemExit):
        bootstrap.main(["--username", "player_one", "--email", "one@example.com"])
    assert "BOOTSTRAP_ARGUMENTS_INVALID" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        bootstrap.main(["--username", "player_one", "--email", "one@example.com", "--role", "player", "--password", "PRIVATE_PASSWORD_ARGUMENT_SENTINEL"])
    output = capsys.readouterr()
    assert output.out == "" and "BOOTSTRAP_ARGUMENTS_INVALID" in output.err
    assert "PRIVATE_PASSWORD_ARGUMENT_SENTINEL" not in output.err
