"""Offline: run with unittest discovery, never the legacy app conftest.

PYTHONPATH=backend python -m unittest discover -s backend/tests/cloudbase -p 'test_gateway.py'
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from src.cloudbase.gateway import (
    ALLOWED_KINDS, CloudBaseGateway, GatewayConflict, GatewayError, GatewayForbidden,
    GatewayHTTPResponse, GatewayNotFound, GatewayUnavailable, MAX_PAYLOAD_BYTES,
    canonical_json, payload_digest, _AiohttpTransport,
)


def response(value, status=200):
    return GatewayHTTPResponse(status, json.dumps(value).encode())


def record(*, version=1, owner="owner-a", key="workspace", payload=None):
    payload = {"plays": []} if payload is None else payload
    return {"kind": "play_workspace", "key": key, "owner": owner,
            "version": version, "payload": payload, "digest": payload_digest(payload)}


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses):
        transport = RecordingTransport(*responses)
        return CloudBaseGateway("offline-test-123", "server-key-placeholder", transport=transport), transport

    async def test_exact_official_rpc_contract(self):
        client, transport = self.client(response({"ok": True, "record": record()}))
        result = await client.get("play_workspace", "workspace", "owner-a")
        self.assertEqual(result, record())
        sent = transport.calls[0]
        self.assertEqual(sent["url"], "https://offline-test-123.api.tcloudbasegateway.com/v1/rdb/rest/rpc/veil_archive_get")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer server-key-placeholder")
        self.assertEqual(sent["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(sent["body"]), {
            "p_kind": "play_workspace", "p_key": "workspace", "p_owner": "owner-a"})
        self.assertLessEqual(sent["timeout_seconds"], 60)

    async def test_missing_record_is_none_but_missing_rpc_is_not(self):
        client, _ = self.client(response({"ok": False, "status": 404, "code": "RECORD_NOT_FOUND"}), response({}, 404))
        self.assertIsNone(await client.get("play_workspace", "workspace", "owner-a"))
        with self.assertRaises(GatewayNotFound) as caught:
            await client.get("play_workspace", "workspace", "owner-a")
        self.assertEqual(caught.exception.code, "GATEWAY_RPC_NOT_FOUND")

    async def test_invalid_record_identity_and_digest_are_rejected(self):
        invalid_records = [record(owner="owner-b"), record(key="someone-else"), record(version=True),
                           {**record(), "digest": "0" * 64}, {**record(), "payload": []}]
        for invalid in invalid_records:
            with self.subTest(invalid=invalid):
                client, _ = self.client(response({"record": invalid}))
                with self.assertRaises(GatewayError):
                    await client.get("play_workspace", "workspace", "owner-a")

    async def test_conflicts_and_forbidden_are_typed_without_remote_secrets(self):
        failures = [
            (response({"ok": False, "status": 409, "code": "VERSION_CONFLICT"}), GatewayConflict),
            (response({"ok": False, "status": 409, "code": "IDEMPOTENCY_CONFLICT"}), GatewayConflict),
            (response({"message": "raw secret"}, 403), GatewayForbidden),
            (response({"message": "raw secret"}, 429), GatewayUnavailable),
            (response({"message": "raw secret"}, 500), GatewayUnavailable),
            (response({"ok": False, "status": 403, "code": "raw secret"}), GatewayForbidden),
        ]
        for reply, expected in failures:
            client, _ = self.client(reply)
            with self.assertRaises(expected) as caught:
                await client.rpc("veil_archive_get", {})
            self.assertNotIn("secret", str(caught.exception))

    async def test_bad_json_and_nondict_responses_fail_closed(self):
        for reply in [GatewayHTTPResponse(200, b"not json"), response([]), response(None), GatewayHTTPResponse(200, b"\xff")]:
            client, _ = self.client(reply)
            with self.assertRaises(GatewayError) as caught:
                await client.rpc("veil_archive_get", {})
            self.assertEqual(caught.exception.code, "GATEWAY_INVALID_RESPONSE")

    async def test_no_automatic_retry_after_unknown_commit(self):
        client, transport = self.client(TimeoutError("secret response"))
        with self.assertRaises(GatewayUnavailable):
            await client.create("play_workspace", "workspace", "owner-a", {}, idempotency_key="request-1")
        self.assertEqual(len(transport.calls), 1)

    async def test_create_supplies_hashes_and_returns_receipt(self):
        client, transport = self.client(response({"ok": True, "record": record(), "replayed": False, "event": None}))
        result = await client.create("play_workspace", "workspace", "owner-a", {"plays": []}, idempotency_key="request-1")
        params = json.loads(transport.calls[0]["body"])
        self.assertEqual(params["p_digest"], payload_digest({"plays": []}))
        self.assertRegex(params["p_request_digest"], "^[0-9a-f]{64}$")
        self.assertFalse(result["replayed"])
        self.assertIsNone(result["event"])

    async def test_cas_replay_keeps_original_record_and_response_event(self):
        original_event = {"command": "advance", "response": {"revision": 1, "visible": ["clue-a"]}}
        original = record(version=2, payload={"revision": 1})
        client, transport = self.client(response({"ok": True, "record": original, "replayed": True, "event": original_event}))
        stable_request_digest = payload_digest({"command": "advance", "idempotency_key": "same-command"})
        # Recomputed work has new UUIDs, but same user command must replay the
        # receipt, not claim those newly computed rows were persisted.
        result = await client.cas_append("play_workspace", "workspace", "owner-a", 4,
            {"revision": 5, "random_uuid": "discard-me"}, {"response": "discard-me"},
            idempotency_key="same-command", request_digest=stable_request_digest)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["record"], original)
        self.assertEqual(result["event"], original_event)
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent["p_expected_version"], 4)
        self.assertEqual(sent["p_request_digest"], stable_request_digest)

    async def test_missing_replayed_flag_is_not_success(self):
        client, _ = self.client(response({"record": record()}))
        with self.assertRaises(GatewayError):
            await client.create("play_workspace", "workspace", "owner-a", {}, idempotency_key="req")

    async def test_bad_kinds_owners_versions_hashes_and_sizes_never_send(self):
        client, transport = self.client()
        actions = [
            lambda: client.get("any_table", "workspace", "owner-a"),
            lambda: client.get("play_workspace", "workspace", ""),
            lambda: client.get("play_workspace", "../../users", "owner-a"),
            lambda: client.create("play_workspace", "workspace", "owner-a", {"body": "x" * MAX_PAYLOAD_BYTES}, idempotency_key="req"),
            lambda: client.create("play_workspace", "workspace", "owner-a", {"nan": float("nan")}, idempotency_key="req"),
            lambda: client.create("play_workspace", "workspace", "owner-a", {}, idempotency_key="bad/key"),
            lambda: client.create("play_workspace", "workspace", "owner-a", {}, idempotency_key="req", request_digest="not sha256"),
            lambda: client.cas_append("play_workspace", "workspace", "owner-a", True, {}, {}, idempotency_key="req"),
            lambda: client.cas_append("play_workspace", "workspace", "owner-a", 0, {}, {}, idempotency_key="req"),
            lambda: client.cas_append("play_workspace", "workspace", "owner-a", 1, {}, {"body": "x" * 65536}, idempotency_key="req"),
            lambda: client.list_records("play_workspace", "owner-a", after=""),
            lambda: client.list_records("play_workspace", "owner-a", limit=26),
        ]
        for action in actions:
            with self.assertRaises(ValueError):
                await action()
        self.assertEqual(transport.calls, [])

    async def test_rpc_cannot_escape_namespace(self):
        client, transport = self.client()
        for name in ["execute_sql", "veil_archive_../../admin", "veil_archive_get?sql=delete", "public.veil_archive_get"]:
            with self.assertRaises(ValueError):
                await client.rpc(name, {})
        self.assertEqual(transport.calls, [])

    async def test_relative_path_and_header_hardening(self):
        client, transport = self.client()
        for path in ["https://attacker.invalid", "//attacker.invalid/a", "/a/../b", "/a/%2e%2e/b", "/a\\b", "/a#b", "/a%0d%0ab", "/%2f%2fattacker.invalid"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                await client.request("GET", path)
        for headers in [{"Authorization": "other-key"}, {"Host": "attacker.invalid"}, {"X-Header": "a\r\nb"}, {"Cookie": "a=b"}]:
            with self.assertRaises(ValueError):
                await client.request("GET", "/v1/storage/object/bucket/file", headers=headers)
        self.assertEqual(transport.calls, [])

    async def test_binary_request_is_fixed_origin_and_bounded(self):
        client, transport = self.client(GatewayHTTPResponse(200, b"image"))
        result = await client.request("POST", "/v1/storage/object/private/key",
            headers={"Content-Type": "application/octet-stream"}, body=b"image", max_response_bytes=512)
        self.assertEqual(result.body, b"image")
        self.assertEqual(transport.calls[0]["body"], b"image")
        self.assertTrue(transport.calls[0]["url"].startswith(client.origin + "/"))
        self.assertEqual(transport.calls[0]["max_response_bytes"], 512)

    async def test_response_bounds_and_redirect_fail_closed(self):
        for reply, limit, code in [(GatewayHTTPResponse(302, b"", {"Location": "https://attacker.invalid"}), 32, "GATEWAY_REDIRECT_REJECTED"),
                                   (GatewayHTTPResponse(200, b"too-large"), 2, "GATEWAY_RESPONSE_TOO_LARGE")]:
            client, transport = self.client(reply)
            with self.assertRaises(GatewayError) as caught:
                await client.request("GET", "/v1/storage/object/private/key", max_response_bytes=limit)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(transport.calls), 1)

    async def test_list_validates_each_record_owner(self):
        client, _ = self.client(response({"records": [record(owner="owner-b")]}))
        with self.assertRaises(GatewayError):
            await client.list_records("play_workspace", "owner-a")

    async def test_default_transport_streams_with_no_redirect_or_environment_proxy(self):
        class Stream:
            async def iter_chunked(self, size):
                self.chunk_size = size
                yield b"ab"
                yield b"cd"

        class Reply:
            status, headers, content = 200, {"Content-Type": "text/plain"}, Stream()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def request(self, *args, **kwargs):
                self.sent = (args, kwargs)
                return Reply()

        session = Session()
        with patch("aiohttp.ClientSession", return_value=session) as constructor:
            result = await _AiohttpTransport().request("POST", "https://fixed.invalid/rpc", headers={},
                body=b"input", timeout_seconds=2, max_response_bytes=4)
            self.assertEqual(result.body, b"abcd")
            self.assertFalse(constructor.call_args.kwargs["trust_env"])
            self.assertEqual(constructor.call_args.kwargs["timeout"].total, 2)
            self.assertFalse(session.sent[1]["allow_redirects"])
            self.assertEqual(session.sent[1]["data"], b"input")
            with self.assertRaises(GatewayError) as caught:
                await _AiohttpTransport().request("GET", "https://fixed.invalid/rpc", headers={},
                    body=None, timeout_seconds=2, max_response_bytes=3)
            self.assertEqual(caught.exception.code, "GATEWAY_RESPONSE_TOO_LARGE")


class CanonicalAndSchemaTests(unittest.TestCase):
    def test_canonical_json_normalizes_key_order_unicode_and_integral_floats(self):
        self.assertEqual(canonical_json({"雾": "幕", "a": [1.0, -0.0]}), '{"a":[1,0],"雾":"幕"}')
        self.assertEqual(payload_digest({"a": 1.0, "b": "中"}), payload_digest({"b": "中", "a": 1}))
        for bad in [{1: "key"}, {"n": float("inf")}, {"a": (1, 2)}, {"a": object()}]:
            with self.assertRaises(ValueError):
                canonical_json(bad)

    def test_constructor_never_contacts_network_and_rejects_origin_injection(self):
        for env in ["https://bad.invalid", "env/other", "ENV-UPPER", "env.bad", "bad\r\nhost"]:
            with self.assertRaises(ValueError):
                CloudBaseGateway(env, "key")
        for key in ["", "key\nvalue", "key value"]:
            with self.assertRaises(ValueError):
                CloudBaseGateway("offline-test-123", key)
        self.assertNotIn("server-key-placeholder", repr(CloudBaseGateway("offline-test-123", "server-key-placeholder")))

    def test_sql_has_null_safe_role_gate_for_all_definer_rpcs(self):
        sql = self.sql()
        self.assertIn("IS DISTINCT FROM 'service_role'", sql)
        blocks = re.findall(r"CREATE OR REPLACE FUNCTION public\.(\w+)\(.*?\n\$\$;", sql, re.S)
        self.assertGreaterEqual(len(blocks), 7)
        for body in re.findall(r"CREATE OR REPLACE FUNCTION public\.\w+\(.*?\n\$\$;", sql, re.S):
            if "SECURITY DEFINER" in body:
                self.assertIn("PERFORM public.veil_archive_assert_service_role();", body)
                self.assertIn("SET search_path = pg_catalog, pg_temp", body)
        self.assertNotRegex(sql, r"(?i)\bEXECUTE\s+(?:p_|format\(|v_sql)")

    def test_sql_cas_replay_precedes_version_and_update(self):
        sql = self.sql().split("CREATE OR REPLACE FUNCTION public.veil_archive_cas_append(", 1)[1]
        self.assertLess(sql.index("FOR UPDATE"), sql.index("SELECT * INTO v_receipt"))
        self.assertLess(sql.index("'replayed', true"), sql.index("IF v_row.version <> p_expected_version"))
        self.assertLess(sql.index("IF v_row.version <> p_expected_version"), sql.index("UPDATE public.veil_archive_records"))
        self.assertIn("v_receipt.request_digest IS DISTINCT FROM p_request_digest", sql)
        self.assertIn("'event', v_receipt.event", sql)

    def test_sql_only_owns_namespaced_objects_and_no_direct_table_grants(self):
        sql = self.sql()
        names = re.findall(r"(?:CREATE TABLE IF NOT EXISTS|CREATE OR REPLACE FUNCTION|ALTER TABLE)\s+([\w.]+)", sql)
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("public.veil_archive_") for name in names))
        self.assertNotRegex(sql, r"(?i)GRANT\s+(SELECT|INSERT|UPDATE|DELETE|ALL).*?ON TABLE")
        self.assertIn("FROM PUBLIC, anon, authenticated, service_role;", sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        for kind in ALLOWED_KINDS:
            self.assertIn(f"'{kind}'", sql)

    @staticmethod
    def sql():
        return (Path(__file__).resolve().parents[3] / "deploy/cloudbase/001_aggregate_store.sql").read_text()


if __name__ == "__main__":
    unittest.main()
