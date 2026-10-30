"""Offline tests; unittest discovery avoids the legacy app/DB conftest.

PYTHONPATH=backend python -B -m unittest discover -s backend/tests/cloudbase -p test_auth.py
SQL concurrency/role tests run separately against an explicitly isolated PG.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from pwdlib import PasswordHash

from src.cloudbase.auth import AuthError, CloudBaseAuth
from src.cloudbase.gateway import GatewayConflict, GatewayError, GatewayUnavailable


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def user(*, identity=1, guest=False, admin=False):
    return {"id": identity, "username": "guest_" + "a" * 32 if guest else "tester",
            "email": None if guest else "tester@example.com", "nickname": "试玩访客" if guest else "Tester",
            "is_active": True, "is_admin": admin, "is_guest": guest}


def issued(**kwargs):
    return {"ok": True, "expires_in": 7200, "user": user(**kwargs)}


class FakePasswords:
    def __init__(self):
        self.verifications = []

    def hash(self, password):
        return "$argon2id$offline$" + digest(password)

    def verify(self, password, hash):
        self.verifications.append((password, hash))
        return self.hash(password) == hash


class RecordingGateway:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def rpc(self, name, params):
        self.calls.append((name, params))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network_guard = patch("socket.socket.connect", side_effect=AssertionError("offline auth test"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def client(self, *replies, passwords=None):
        gateway = RecordingGateway(*replies)
        passwords = passwords or FakePasswords()
        return CloudBaseAuth(gateway, passwords=passwords), gateway, passwords

    async def test_guests_are_separate_and_only_hashes_reach_gateway(self):
        auth, gateway, _ = self.client(issued(guest=True), issued(guest=True, identity=2))
        first = await auth.guest("127.0.0.1")
        second = await auth.guest("127.0.0.1")
        self.assertNotEqual(first["user"]["id"], second["user"]["id"])
        self.assertNotEqual(first["access_token"], second["access_token"])
        self.assertNotEqual(first["refresh_token"], second["refresh_token"])
        self.assertNotEqual(gateway.calls[0][1]["p_username"], gateway.calls[1][1]["p_username"])
        for result, (name, params) in zip((first, second), gateway.calls):
            self.assertEqual(name, "veil_archive_auth_guest")
            self.assertRegex(params["p_username"], r"^guest_[a-f0-9]{32}$")
            self.assertEqual(params["p_access_hash"], digest(result["access_token"]))
            self.assertEqual(params["p_refresh_hash"], digest(result["refresh_token"]))
            for key in ("p_access_hash", "p_refresh_hash", "p_client_hash"):
                self.assertRegex(params[key], r"^[a-f0-9]{64}$")
            serialized = json.dumps(params)
            self.assertNotIn(result["access_token"], serialized)
            self.assertNotIn(result["refresh_token"], serialized)
            self.assertNotIn("127.0.0.1", serialized)
            self.assertFalse(result["user"]["is_admin"])

    async def test_login_checks_password_before_issue_and_binds_current_hash(self):
        passwords = FakePasswords()
        encoded = passwords.hash("correct-test-password")
        credentials = {"ok": True, "user": {**user(), "password_hash": encoded}, "password_hash": encoded}
        auth, gateway, _ = self.client(credentials, {**issued(), "password_hash": encoded}, passwords=passwords)
        result = await auth.login(" TESTER@EXAMPLE.COM ", "correct-test-password", "peer")
        self.assertEqual([call[0] for call in gateway.calls], [
            "veil_archive_auth_credentials", "veil_archive_auth_login_complete"])
        lookup, complete = (call[1] for call in gateway.calls)
        self.assertEqual(lookup["p_login"], "tester@example.com")
        self.assertEqual(lookup["p_login_hash"], complete["p_login_hash"])
        self.assertEqual(complete["p_expected_password_hash"], encoded)
        self.assertEqual(complete["p_user_id"], 1)
        self.assertEqual(complete["p_access_hash"], digest(result["access_token"]))
        self.assertNotIn("password_hash", json.dumps(result))
        self.assertNotIn("correct-test-password", json.dumps(gateway.calls))

    async def test_wrong_missing_inactive_accounts_do_not_issue(self):
        passwords = FakePasswords()
        cases = [
            {"ok": True, "user": user(), "password_hash": passwords.hash("different")},
            {"ok": True, "user": None, "password_hash": None},
            {"ok": True, "user": {**user(), "is_active": False}, "password_hash": None},
        ]
        for credentials in cases:
            with self.subTest(credentials=credentials):
                auth, gateway, hasher = self.client(credentials)
                with self.assertRaises(AuthError) as caught:
                    await auth.login("tester", "wrong-test-password", "peer")
                self.assertEqual((caught.exception.code, caught.exception.status), ("AUTH_INVALID_CREDENTIALS", 401))
                self.assertEqual(len(gateway.calls), 1)
                self.assertEqual(len(hasher.verifications), 1)

    async def test_rate_limit_rejection_fails_closed(self):
        auth, gateway, passwords = self.client(GatewayError("AUTH_LOGIN_RATE_LIMIT", 429))
        with self.assertRaises(GatewayError) as caught:
            await auth.login("tester", "password", "peer")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(passwords.verifications, [])

    async def test_refresh_is_one_atomic_rpc_with_no_raw_tokens(self):
        auth, gateway, _ = self.client(issued())
        old = "vr_" + "r" * 64
        result = await auth.refresh(old, "peer")
        self.assertEqual(len(gateway.calls), 1)
        name, params = gateway.calls[0]
        self.assertEqual(name, "veil_archive_auth_refresh")
        self.assertEqual(params["p_old_refresh_hash"], digest(old))
        self.assertEqual(params["p_access_hash"], digest(result["access_token"]))
        self.assertEqual(params["p_refresh_hash"], digest(result["refresh_token"]))
        self.assertNotIn(old, json.dumps(params))
        self.assertNotEqual(result["refresh_token"], old)

    async def test_uncertain_refresh_or_guest_write_never_retries(self):
        for method, arguments in (("refresh", ("vr_" + "r" * 64, "peer")), ("guest", ("peer",))):
            with self.subTest(method=method):
                auth, gateway, _ = self.client(GatewayUnavailable())
                with self.assertRaises(GatewayUnavailable):
                    await getattr(auth, method)(*arguments)
                self.assertEqual(len(gateway.calls), 1)

    async def test_me_logout_bind_access_hash_and_whitelist_user_fields(self):
        auth, gateway, _ = self.client({"ok": True, "user": {**user(), "password_hash": "never expose"}},
                                      {"ok": True, "token_revoked": True, "secret": "never expose"})
        token = "va_" + "a" * 64
        self.assertEqual(await auth.me(token), user())
        self.assertEqual(await auth.logout(token), {"message": "登出成功", "token_revoked": True})
        self.assertEqual(gateway.calls, [
            ("veil_archive_auth_me", {"p_access_hash": digest(token)}),
            ("veil_archive_auth_logout", {"p_access_hash": digest(token)}),
        ])

    async def test_token_type_and_invalid_inputs_rejected_before_rpc(self):
        auth, gateway, _ = self.client()
        calls = [auth.me("vr_" + "r" * 64), auth.refresh("va_" + "a" * 64, "peer"),
                 auth.logout("not-a-token"), auth.guest(""), auth.guest("peer\nspoof"),
                 auth.login("tester", "p" * 101, "peer"), auth.login("\nadmin", "password", "peer")]
        for call in calls:
            with self.assertRaises(AuthError):
                await call
        self.assertEqual(gateway.calls, [])

    async def test_malformed_or_guest_admin_responses_are_rejected(self):
        invalid = [issued(admin=True, guest=True), {**issued(), "expires_in": True},
                   {**issued(), "expires_in": 7201}, {**issued(), "user": {**user(), "id": True}},
                   {**issued(), "user": {**user(), "is_active": False}},
                   {**issued(), "user": {**user(), "username": "../../escape"}}]
        for reply in invalid:
            with self.subTest(reply=reply):
                auth, gateway, _ = self.client(reply)
                with self.assertRaises(GatewayError):
                    await auth.guest("peer")
                self.assertEqual(len(gateway.calls), 1)

    async def test_bootstrap_defaults_player_and_admin_is_explicit(self):
        auth, gateway, _ = self.client({"ok": True, "user": user()}, {"ok": True, "user": user(admin=True)})
        await auth.bootstrap("tester", "TESTER@example.com", "unique-test-password")
        await auth.bootstrap("tester", "tester@example.com", "unique-test-password", role="admin")
        self.assertFalse(gateway.calls[0][1]["p_is_admin"])
        self.assertTrue(gateway.calls[1][1]["p_is_admin"])
        self.assertEqual(gateway.calls[0][1]["p_email"], "tester@example.com")
        self.assertTrue(gateway.calls[0][1]["p_password_hash"].startswith("$argon2id$"))
        self.assertNotIn("unique-test-password", json.dumps(gateway.calls))

    async def test_existing_account_conflict_does_not_retry_or_update(self):
        auth, gateway, _ = self.client(GatewayConflict("AUTH_ACCOUNT_EXISTS"))
        with self.assertRaises(GatewayConflict):
            await auth.bootstrap("tester", "tester@example.com", "unique-test-password", role="admin")
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.calls[0][0], "veil_archive_auth_bootstrap")

    async def test_bootstrap_uses_real_argon2_hash_only_server_side(self):
        auth, gateway, _ = self.client({"ok": True, "user": user()}, passwords=PasswordHash.recommended())
        await auth.bootstrap("tester", "tester@example.com", "unique-test-password")
        encoded = gateway.calls[0][1]["p_password_hash"]
        self.assertTrue(PasswordHash.recommended().verify("unique-test-password", encoded))
        self.assertNotIn("unique-test-password", encoded)

    async def test_bootstrap_invalid_inputs_never_call_gateway(self):
        auth, gateway, _ = self.client()
        cases = [dict(username="UPPER"), dict(username="../escape"), dict(role="owner"),
                 dict(email="no-email"), dict(password="short"), dict(password="bad\npassword"),
                 dict(password="p" * 101)]
        for change in cases:
            with self.subTest(change=change):
                values = {"username": "tester", "email": "tester@example.com", "password": "unique-test-password", **change}
                with self.assertRaises(AuthError):
                    await auth.bootstrap(**values)
        self.assertEqual(gateway.calls, [])


class AuthSQLContractTests(unittest.TestCase):
    def test_every_auth_definer_checks_service_role_and_browser_grants_are_revoked(self):
        sql = (Path(__file__).resolve().parents[3] / "deploy/cloudbase/002_auth.sql").read_text()
        functions = re.findall(r"CREATE OR REPLACE FUNCTION (.*?)END \$\$;", sql, re.DOTALL)
        self.assertEqual(len(functions), 10)
        for function in functions:
            self.assertIn("SECURITY DEFINER SET search_path = pg_catalog", function)
            self.assertIn("PERFORM public.veil_archive_assert_service_role();", function)
        self.assertIn("FROM PUBLIC, anon, authenticated, service_role", sql)
        self.assertIn("FROM PUBLIC, anon, authenticated',f.signature", sql)
        self.assertNotRegex(sql, r"(?i)(?:UPDATE|INSERT INTO|DELETE FROM)\s+(?:public\.)?users\b")

    def test_refresh_and_bootstrap_have_transactional_guards(self):
        sql = (Path(__file__).resolve().parents[3] / "deploy/cloudbase/002_auth.sql").read_text()
        refresh = sql.split("FUNCTION public.veil_archive_auth_refresh(", 1)[1].split("END $$;", 1)[0]
        self.assertIn("refresh_hash=p_old_refresh_hash FOR UPDATE", refresh)
        self.assertIn("v_old.revoked_at IS NOT NULL", refresh)
        self.assertIn("rotated_to=v_new_id", refresh)
        bootstrap = sql.split("FUNCTION public.veil_archive_auth_bootstrap(", 1)[1].split("END $$;", 1)[0]
        self.assertIn("p_is_admin boolean DEFAULT false", bootstrap)
        self.assertIn("ON CONFLICT DO NOTHING", bootstrap)
        self.assertIn("AUTH_ACCOUNT_EXISTS", bootstrap)
        self.assertNotIn("DO UPDATE", bootstrap)


if __name__ == "__main__":
    unittest.main()
