"""CloudBase-only identities, with opaque tokens and authoritative PG state.

Only this server component receives the service-role gateway. Browsers receive
random bearer tokens, never the gateway API key, password hashes or SQL access.
There is intentionally no in-memory/Redis fallback and no automatic RPC retry.
The caller must derive ``client_id`` from its trusted request peer, not a client
supplied header or JSON field. Bootstrap is a server/console operation, not a
public HTTP endpoint. Existing production AuthService and users are untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from typing import Any, Protocol
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from .gateway import GatewayError


_USERNAME = re.compile(r"[a-z0-9_]{3,50}\Z")
_TOKEN = re.compile(r"v[ar]_[A-Za-z0-9_-]{64}\Z")


class AuthError(GatewayError):
    """Sanitized auth rejection, compatible with the gateway error handler."""

    def __init__(self, code: str = "AUTH_INVALID_CREDENTIALS", status: int = 401) -> None:
        super().__init__(code, status)


class AuthGateway(Protocol):
    async def rpc(self, name: str, params: dict[str, Any]) -> dict[str, Any]: ...


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password: str, hash: str) -> bool: ...


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _client_hash(client_id: str) -> str:
    if (not isinstance(client_id, str) or not 1 <= len(client_id) <= 256
            or any(ord(char) < 32 for char in client_id)):
        raise AuthError("AUTH_CLIENT_INVALID", 400)
    return _digest("client\0" + client_id)


def _checked_token(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value) or not value.startswith(prefix):
        raise AuthError("AUTH_SESSION_INVALID")
    return _digest(value)


def _user(result: dict[str, Any]) -> dict[str, Any]:
    """Whitelist DTO fields; a credential RPC response must never escape."""
    value = result.get("user")
    if (not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0
            or not isinstance(value.get("username"), str) or not _USERNAME.fullmatch(value["username"])
            or not isinstance(value.get("nickname"), str)
            or type(value.get("is_admin")) is not bool
            or type(value.get("is_active")) is not bool
            or type(value.get("is_guest")) is not bool
            or (value.get("email") is not None and not isinstance(value["email"], str))
            or (value["is_guest"] and value["is_admin"])):
        raise GatewayError("AUTH_RESPONSE_INVALID")
    if not value["is_active"]:
        raise AuthError("AUTH_SESSION_INVALID")
    return {name: value.get(name) for name in (
        "id", "username", "email", "nickname", "is_admin", "is_active", "is_guest",
    )}


def _tokens() -> tuple[str, str]:
    # 48 random bytes per token. Prefixes distinguish access from refresh tokens.
    return "va_" + secrets.token_urlsafe(48), "vr_" + secrets.token_urlsafe(48)


def _token_response(result: dict[str, Any], access: str, refresh: str) -> dict[str, Any]:
    expires = result.get("expires_in")
    if type(expires) is not int or not 1 <= expires <= 7200:
        raise GatewayError("AUTH_RESPONSE_INVALID")
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
            "expires_in": expires, "user": _user(result)}


class CloudBaseAuth:
    def __init__(self, gateway: AuthGateway, *, passwords: PasswordHasher | None = None) -> None:
        self._gateway = gateway
        self._passwords = passwords or PasswordHash.recommended()
        # This is deliberately not an account password. Equal-cost verification
        # for missing/inactive accounts avoids a fast username-existence oracle.
        self._dummy_hash = self._passwords.hash("veil-auth-non-account-" + secrets.token_urlsafe(24))

    async def guest(self, client_id: str) -> dict[str, Any]:
        client_hash = _client_hash(client_id)
        access, refresh = _tokens()
        result = await self._gateway.rpc("veil_archive_auth_guest", {
            "p_username": "guest_" + uuid4().hex, "p_access_hash": _digest(access),
            "p_refresh_hash": _digest(refresh), "p_client_hash": client_hash,
        })
        return _token_response(result, access, refresh)

    async def login(self, username: str, password: str, client_id: str) -> dict[str, Any]:
        client_hash = _client_hash(client_id)
        if (not isinstance(username, str) or not 1 <= len(username.strip()) <= 100
                or not isinstance(password, str) or not 1 <= len(password) <= 100
                or any(ord(char) < 32 for char in username)):
            raise AuthError()
        login = username.strip().lower()
        login_hash = _digest("login\0" + login)
        result = await self._gateway.rpc("veil_archive_auth_credentials", {
            "p_login": login, "p_login_hash": login_hash, "p_client_hash": client_hash,
        })
        encoded = result.get("password_hash")
        valid_hash = isinstance(encoded, str) and encoded.startswith("$argon2id$") and len(encoded) <= 512
        verification_hash = encoded if isinstance(encoded, str) and valid_hash else self._dummy_hash
        try:
            valid = await asyncio.to_thread(self._passwords.verify, password, verification_hash)
        except (UnknownHashError, ValueError):
            valid = False
        if not valid_hash or not valid:
            raise AuthError()
        user = _user(result)
        if not user["is_active"] or user["is_guest"]:
            raise AuthError()
        access, refresh = _tokens()
        issued = await self._gateway.rpc("veil_archive_auth_login_complete", {
            "p_user_id": user["id"], "p_expected_password_hash": encoded,
            "p_login_hash": login_hash, "p_access_hash": _digest(access),
            "p_refresh_hash": _digest(refresh),
        })
        return _token_response(issued, access, refresh)

    async def me(self, access_token: str) -> dict[str, Any]:
        result = await self._gateway.rpc("veil_archive_auth_me", {
            "p_access_hash": _checked_token(access_token, "va_"),
        })
        return _user(result)

    async def refresh(self, refresh_token: str, client_id: str) -> dict[str, Any]:
        old_hash = _checked_token(refresh_token, "vr_")
        client_hash = _client_hash(client_id)
        access, refresh = _tokens()
        # The SQL RPC locks and consumes the old session and creates its successor
        # in one transaction. A lost response is never retried with new tokens.
        result = await self._gateway.rpc("veil_archive_auth_refresh", {
            "p_old_refresh_hash": old_hash, "p_access_hash": _digest(access),
            "p_refresh_hash": _digest(refresh), "p_client_hash": client_hash,
        })
        return _token_response(result, access, refresh)

    async def logout(self, access_token: str) -> dict[str, Any]:
        result = await self._gateway.rpc("veil_archive_auth_logout", {
            "p_access_hash": _checked_token(access_token, "va_"),
        })
        if type(result.get("token_revoked")) is not bool:
            raise GatewayError("AUTH_RESPONSE_INVALID")
        return {"message": "登出成功", "token_revoked": result["token_revoked"]}

    async def bootstrap(self, username: str, email: str, password: str, *, role: str = "player") -> dict[str, Any]:
        """Insert one new account; conflicts never reset a password or grant admin.

        Invoke only from a server-side operator command with getpass/safe stdin.
        Do not mount this method as a public registration/bootstrap route.
        """
        if (not isinstance(username, str) or not _USERNAME.fullmatch(username)
                or role not in {"admin", "player"}
                or not isinstance(password, str) or not 12 <= len(password) <= 100
                or any(ord(char) < 32 or ord(char) == 127 for char in password)
                or not isinstance(email, str) or len(email) > 100):
            raise AuthError("AUTH_BOOTSTRAP_INPUT_INVALID", 400)
        try:
            normalized_email = validate_email(email, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            raise AuthError("AUTH_BOOTSTRAP_INPUT_INVALID", 400) from None
        if len(normalized_email) > 100:
            raise AuthError("AUTH_BOOTSTRAP_INPUT_INVALID", 400)
        encoded = await asyncio.to_thread(self._passwords.hash, password)
        if not isinstance(encoded, str) or not encoded.startswith("$argon2id$") or len(encoded) > 512:
            raise GatewayError("AUTH_PASSWORD_HASH_INVALID")
        result = await self._gateway.rpc("veil_archive_auth_bootstrap", {
            "p_username": username, "p_email": normalized_email,
            "p_password_hash": encoded, "p_is_admin": role == "admin",
        })
        return _user(result)
