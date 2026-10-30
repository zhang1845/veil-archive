"""Offline HTTP contract tests with injected auth/gateway/play dependencies."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient

from src.cloudbase.app import active_user, create_app
from src.cloudbase.gateway import GatewayError, GatewayUnavailable
from src.fusion.package_play import PackagePlayError


ACCESS = "va_" + "a" * 64
REFRESH = "vr_" + "r" * 64
OPENING = "package-" + "b" * 32
PLAY = "play-" + "c" * 32
HEADERS = {"Authorization": "Bearer " + ACCESS}
USER = {"id": 7, "username": "tester", "nickname": "Tester", "is_active": True, "is_admin": False}
TOKEN = {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "bearer", "expires_in": 7200, "user": USER}


class FakeAuth:
    def __init__(self):
        self.calls = []
        self.error = None

    async def guest(self, client_id):
        self.calls.append(("guest", client_id))
        return TOKEN

    async def login(self, username, password, client_id):
        self.calls.append(("login", username, password, client_id))
        return TOKEN

    async def refresh(self, refresh, client_id):
        self.calls.append(("refresh", refresh, client_id))
        return TOKEN

    async def me(self, token):
        self.calls.append(("me", token))
        if self.error is not None:
            raise self.error
        if token != ACCESS:
            raise GatewayError("AUTH_SESSION_INVALID", 401)
        return USER

    async def logout(self, token):
        self.calls.append(("logout", token))
        return {"message": "登出成功", "token_revoked": True}


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.record = {"payload": {"test_fixture": True}}
        self.error = None

    async def get(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.record


class FakePlay:
    def __init__(self):
        self.calls = []
        self.error = None

    async def run(self, owner, operation, body):
        self.calls.append((owner, operation, body))
        if self.error is not None:
            raise self.error
        return {"operation": operation, "owner": owner, "body": body}

    async def catalog(self):
        self.calls.append(("catalog",))
        if self.error is not None:
            raise self.error
        return [{"id": 1, "title": "公开目录测试"}]


class AppTests(unittest.TestCase):
    def setUp(self):
        self.auth, self.gateway, self.play = FakeAuth(), FakeGateway(), FakePlay()
        self.env = patch.dict("os.environ", {"CORS_ORIGINS": "https://play.example.test,http://127.0.0.1:3001"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch("socket.socket.connect", side_effect=AssertionError("offline app test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.app = create_app(gateway=self.gateway, auth=self.auth, manifests=[])
        self.app.state.cloudbase_play = self.play
        self.client = TestClient(self.app, client=("127.0.0.1", 5555))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def assert_private(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_auth_routes_return_direct_token_and_ignore_forwarded_identity(self):
        response = self.client.post("/api/auth/anonymous-login", headers={"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "2.3.4.5"})
        self.assert_private(response)
        self.assertEqual(response.json(), TOKEN)
        self.assertEqual(self.auth.calls[-1], ("guest", "127.0.0.1"))
        response = self.client.post("/api/auth/login", json={"username": "tester", "password": "test-password"})
        self.assert_private(response)
        self.assertEqual(response.json(), TOKEN)
        self.assertEqual(self.auth.calls[-1], ("login", "tester", "test-password", "127.0.0.1"))
        response = self.client.post("/api/auth/refresh", json={"refresh_token": REFRESH})
        self.assert_private(response)
        self.assertEqual(self.auth.calls[-1], ("refresh", REFRESH, "127.0.0.1"))
        response = self.client.get("/api/auth/me", headers=HEADERS)
        self.assert_private(response)
        self.assertEqual(response.json(), USER)
        response = self.client.post("/api/auth/logout", headers=HEADERS)
        self.assert_private(response)
        self.assertEqual(response.json()["token_revoked"], True)
        self.assertEqual(self.auth.calls[-1], ("logout", ACCESS))

    def test_frontend_fusion_routes_bind_owner_and_match_envelopes(self):
        routes = [
            ("post", "/api/fusion/package-sessions", {"release_id": 1, "character_id": "detective", "idempotency_key": "open-1"}, "opening.create", None, 201),
            ("get", f"/api/fusion/package-sessions/{OPENING}", None, "opening.get", {"session_id": OPENING}, 200),
            ("post", "/api/fusion/package-plays", {"opening_session_id": OPENING, "idempotency_key": "play-1"}, "play.create", None, 201),
            ("get", f"/api/fusion/package-plays?opening_session_id={OPENING}", None, "play.find", {"opening_session_id": OPENING}, 200),
            ("get", f"/api/fusion/package-plays/{PLAY}", None, "play.get", {"play_id": PLAY}, 200),
            ("get", "/api/fusion/package-play-library?offset=20&limit=10", None, "play.library", {"offset": 20, "limit": 10}, 200),
        ]
        for method, path, body, operation, expected_body, status in routes:
            with self.subTest(path=path):
                kwargs = {"headers": HEADERS}
                if body is not None:
                    kwargs["json"] = body
                response = getattr(self.client, method)(path, **kwargs)
                self.assert_private(response, status)
                actual = expected_body if expected_body is not None else body
                self.assertEqual(response.json(), {"success": True, "data": {"operation": operation, "owner": 7, "body": actual}})
                self.assertEqual(self.play.calls[-1], (7, operation, actual))

    def test_public_catalog_requires_no_login_or_guest_creation(self):
        self.auth.error = GatewayError("AUTH_SESSION_INVALID", 401)
        for headers in ({}, {"Authorization": "Bearer expired"}):
            response = self.client.get("/api/fusion/package-releases", headers=headers)
            self.assert_private(response)
            self.assertEqual(response.json(), {"success": True, "data": [{"id": 1, "title": "公开目录测试"}]})
        self.assertEqual(self.auth.calls, [])
        self.assertEqual(self.play.calls, [("catalog",), ("catalog",)])

    def test_action_and_discussion_use_strict_commands_and_idempotency(self):
        action = {"action": "ADVANCE_PHASE", "expected_revision": 0, "idempotency_key": "advance-1"}
        response = self.client.post(f"/api/fusion/package-plays/{PLAY}/actions", headers=HEADERS, json=action)
        self.assert_private(response)
        self.assertEqual(self.play.calls[-1], (7, "play.act", {"play_id": PLAY, "command": action}))
        speak = {"schema_version": "package-discussion-command/1.0", "action": "SPEAK", "expected_revision": 1,
                 "idempotency_key": "speak-1", "text": "  我发现了一张车票。  "}
        response = self.client.post(f"/api/fusion/package-plays/{PLAY}/discussion", headers=HEADERS, json=speak)
        self.assert_private(response)
        self.assertEqual(self.play.calls[-1], (7, "play.speak", {"play_id": PLAY, "command": {**speak, "text": "我发现了一张车票。"}}))

    def test_missing_invalid_duplicate_auth_cannot_access_player_data(self):
        for headers in ({}, {"Authorization": "Bearer bad"}, {"Authorization": "Bearer a b"},
                        {"Authorization": "Basic anything"}, [("Authorization", "Bearer " + ACCESS), ("Authorization", "Bearer " + ACCESS)]):
            response = self.client.get("/api/fusion/package-play-library", headers=headers)
            self.assert_private(response, 401)
            self.assertEqual(response.json(), {"detail": "AUTH_SESSION_INVALID"})
        self.assertEqual(self.play.calls, [])

    def test_strict_json_duplicate_fields_and_extra_identity_are_rejected(self):
        requests = [
            ("/api/auth/login", b'{"username":"a","username":"b","password":"secret-test-value"}'),
            ("/api/auth/login", b'{"username":"a","password":"secret-test-value","is_admin":true}'),
            ("/api/auth/login", b'{"username":123,"password":"secret-test-value"}'),
            ("/api/auth/anonymous-login", b'{"username":"shared_guest"}'),
            ("/api/fusion/package-sessions", b'{"release_id":true,"character_id":"a","idempotency_key":"a"}'),
            ("/api/fusion/package-sessions", b'{"release_id":1,"character_id":"a","idempotency_key":"a","owner_user_id":8}'),
        ]
        for path, content in requests:
            with self.subTest(path=path, content=content):
                response = self.client.post(path, headers={**HEADERS, "Content-Type": "application/json"}, content=content)
                self.assert_private(response, 422)
                self.assertNotIn("secret-test-value", response.text)
        self.assertEqual(self.play.calls, [])
        self.assertFalse(any(call[0] in {"guest", "login"} for call in self.auth.calls))

    def test_body_byte_limit_type_and_parse_failures_are_private(self):
        response = self.client.post("/api/auth/login", content=b"x" * 4097, headers={"Content-Type": "application/json"})
        self.assert_private(response, 413)
        response = self.client.post("/api/auth/login", content="username=tester&password=secret-test-value", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assert_private(response, 415)
        for content in (b"not-json", b'{"username":"x","password":NaN}', b'{"username":"\\ud800","password":"x"}'):
            response = self.client.post("/api/auth/login", content=content, headers={"Content-Type": "application/json"})
            self.assert_private(response, 422)
        self.assertEqual(self.auth.calls, [])

    def test_invalid_paths_pagination_and_huge_queries_do_not_run_play(self):
        paths = ["/api/fusion/package-sessions/invalid", "/api/fusion/package-plays/invalid",
                 "/api/fusion/package-plays?opening_session_id=invalid", "/api/fusion/package-play-library?limit=0",
                 "/api/fusion/package-play-library?offset=-1", "/api/fusion/package-play-library?limit=５",
                 "/api/fusion/package-play-library?offset=99999999999"]
        for path in paths:
            response = self.client.get(path, headers=HEADERS)
            self.assert_private(response, 422)
        response = self.client.get("/api/fusion/package-releases?junk=" + "x" * 2100, headers=HEADERS)
        self.assert_private(response, 413)
        self.assertEqual(self.play.calls, [])

    def test_gateway_rule_and_unexpected_errors_are_sanitized(self):
        cases = [(GatewayError("AUTH_LOGIN_RATE_LIMIT", 429), 429, "AUTH_LOGIN_RATE_LIMIT"),
                 (GatewayError("secret-test-value", 503), 503, "GATEWAY_ERROR"),
                 (PackagePlayError("PACKAGE_PLAY_REVISION_CONFLICT", 409), 409, "PACKAGE_PLAY_REVISION_CONFLICT"),
                 (RuntimeError("secret-test-value"), 500, "CLOUDBASE_REQUEST_FAILED")]
        for error, status, code in cases:
            self.play.error = error
            response = self.client.get("/api/fusion/package-releases", headers=HEADERS)
            self.assert_private(response, status)
            self.assertEqual(response.json(), {"detail": code})
            self.assertNotIn("secret-test-value", response.text)

    def test_health_reads_and_validates_live_manifest_on_every_request(self):
        with patch("src.cloudbase.app.ManifestReader") as validate:
            for _ in range(2):
                response = self.client.get("/health")
                self.assert_private(response)
                self.assertEqual(response.json()["status"], "ok")
            self.assertEqual(self.gateway.calls, [("preview_manifest", "rain-seventh-code-v2", "system")] * 2)
            self.assertEqual(validate.call_count, 2)
            validate.assert_called_with([self.gateway.record["payload"]])
        self.gateway.error = GatewayUnavailable()
        response = self.client.get("/health")
        self.assert_private(response, 503)
        self.assertEqual(response.json(), {"detail": "CLOUDBASE_DEPENDENCY_UNAVAILABLE"})

    def test_health_rejects_missing_or_forged_manifest(self):
        for record in (None, {"payload": {"status": "APPROVED"}}):
            self.gateway.record = record
            response = self.client.get("/health")
            self.assert_private(response, 503)
            self.assertEqual(response.json(), {"detail": "CLOUDBASE_DEPENDENCY_UNAVAILABLE"})

    def test_cors_exact_allowlist_without_credentials_and_private_preflight(self):
        for origin in ("https://play.example.test", "http://127.0.0.1:3001"):
            response = self.client.options("/api/auth/login", headers={"Origin": origin,
                "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
            self.assert_private(response)
            self.assertEqual(response.headers["access-control-allow-origin"], origin)
            self.assertNotIn("access-control-allow-credentials", response.headers)
        response = self.client.options("/api/auth/login", headers={"Origin": "https://evil.example.test",
            "Access-Control-Request-Method": "POST"})
        self.assert_private(response, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_no_legacy_admin_paid_or_source_routes_are_accidentally_mounted(self):
        for path in ("/docs", "/openapi.json", "/api/auth/register", "/api/auth/sms-code", "/api/admin/source-bundles",
                     f"/api/fusion/package-plays/{PLAY}/ask", "/api/admin/bootstrap"):
            response = self.client.post(path, json={}) if path.startswith("/api/") else self.client.get(path)
            self.assert_private(response, 404)
            self.assertEqual(response.json(), {"detail": "CLOUDBASE_ENDPOINT_NOT_AVAILABLE"})

    def test_admin_router_can_be_added_using_exported_identity_dependency(self):
        router = APIRouter()

        @router.get("/test-extension/admin")
        async def admin(user: dict = Depends(active_user)):
            if user.get("is_admin") is not True:
                raise HTTPException(403, "ADMIN_REQUIRED")
            return {"id": user["id"]}

        self.app.include_router(router)
        response = self.client.get("/test-extension/admin", headers=HEADERS)
        self.assert_private(response, 403)
        self.assertEqual(response.json(), {"detail": "ADMIN_REQUIRED"})

    def test_invalid_cors_configuration_fails_at_factory_without_network(self):
        for origin in ("*", "http://public.example.test", "https://good.example.test/path", "https://user:password@example.test",
                       "https://*.example.test", "https://example.test?x=1", "https://example.test\\@evil.test"):
            with self.subTest(origin=origin), patch.dict("os.environ", {"CORS_ORIGINS": origin}):
                with self.assertRaisesRegex(ValueError, "CLOUDBASE_CORS_INVALID"):
                    create_app(gateway=self.gateway, auth=self.auth, manifests=[])

    def test_app_requires_explicit_gateway_configuration_without_dotenv(self):
        with patch.dict("os.environ", {"CLOUDBASE_ENV_ID": "", "CLOUDBASE_API_KEY": "", "CLOUDBASE_APIKEY": ""}), patch("dotenv.load_dotenv", side_effect=AssertionError("do not load env")):
            with self.assertRaisesRegex(RuntimeError, "CLOUDBASE_CONFIG_REQUIRED"):
                create_app()

    def test_official_secure_api_key_alias_and_explicit_key_precedence(self):
        for explicit, selected in (("", "synthetic-bound-key"), ("synthetic-explicit-key", "synthetic-explicit-key")):
            with patch.dict("os.environ", {"CLOUDBASE_ENV_ID": "offline-test-123", "CLOUDBASE_API_KEY": explicit,
                    "CLOUDBASE_APIKEY": "synthetic-bound-key", "CORS_ORIGINS": ""}), patch("src.cloudbase.app.CloudBaseGateway") as gateway:
                create_app(auth=self.auth, manifests=[])
                gateway.assert_called_once_with("offline-test-123", selected)


if __name__ == "__main__":
    unittest.main()
