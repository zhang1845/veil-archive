"""Private-input offline rules/persistence and HTTP integration acceptance.

Set CLOUDBASE_TEST_PACKAGE_FILE to the explicitly authorized original candidate.
The exact allowlisted hash is mandatory. No private case text is copied into
this file or printed on assertion failures. The fake gateway models JSON/CAS
and receipts only for tests; production always uses PostgreSQL transaction RPCs.
Real PostgreSQL concurrency is covered separately by test_pg_transactions.py.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.cloudbase.app import create_app
from src.cloudbase.gateway import (GatewayConflict, GatewayUnavailable, MAX_EVENT_BYTES,
                                   MAX_PAYLOAD_BYTES, canonical_json, payload_digest)
from src.cloudbase.play import CloudBasePlay, ORIGINAL_PACKAGE_HASH, Workspace, original_preview
from src.fusion.package_play import PackagePlayError
from src.fusion.package_runtime import PackageRuntimeError
from src.fusion.package_validation import content_hash, parse_package_json


PACKAGE_FILE = os.environ.get("CLOUDBASE_TEST_PACKAGE_FILE", "")


def json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class DurableTestGateway:
    """Test-only JSON durable store, with SQL receipt-before-CAS semantics."""
    def __init__(self, manifest):
        self.manifest = json_copy(manifest)
        self.records = {}
        self.receipts = {}
        self.commits = 0
        self.fail_before = False
        self.fail_after = False
        self.yield_before_cas = False

    async def get(self, kind, key, owner):
        if (kind, key, owner) == ("preview_manifest", "rain-seventh-code-v2", "system"):
            return {"payload": json_copy(self.manifest)}
        return json_copy(self.records.get((kind, key, owner)))

    async def create(self, kind, key, owner, payload, *, idempotency_key, request_digest=None):
        record_key = (kind, key, owner)
        receipt_key = (*record_key, idempotency_key)
        if receipt_key in self.receipts:
            return json_copy(self.receipts[receipt_key][1])
        if record_key in self.records:
            raise GatewayConflict("RECORD_EXISTS")
        record = {"kind": kind, "key": key, "owner": owner, "version": 1,
                  "payload": json_copy(payload), "digest": payload_digest(payload)}
        self.records[record_key] = record
        result = {"record": record, "event": None, "replayed": False}
        self.receipts[receipt_key] = (request_digest, json_copy(result))
        return json_copy(result)

    async def cas_append(self, kind, key, owner, expected_version, payload, event, *, idempotency_key, request_digest):
        if (len(canonical_json(payload).encode()) > MAX_PAYLOAD_BYTES
                or len(canonical_json(event).encode()) > MAX_EVENT_BYTES):
            raise ValueError("TEST_GATEWAY_BOUNDED_PAYLOAD_EXCEEDED")
        if self.yield_before_cas:
            await asyncio.sleep(0)
        if self.fail_before:
            self.fail_before = False
            raise GatewayUnavailable()
        record_key = (kind, key, owner)
        receipt_key = (*record_key, idempotency_key)
        if receipt_key in self.receipts:
            digest, previous = self.receipts[receipt_key]
            if digest != request_digest:
                raise GatewayConflict("IDEMPOTENCY_CONFLICT")
            return {**json_copy(previous), "replayed": True}
        previous = self.records[record_key]
        if previous["version"] != expected_version:
            raise GatewayConflict("VERSION_CONFLICT")
        record = {**previous, "version": expected_version + 1,
                  "payload": json_copy(payload), "digest": payload_digest(payload)}
        self.records[record_key] = record
        result = {"record": record, "event": json_copy(event), "replayed": False}
        self.receipts[receipt_key] = (request_digest, json_copy(result))
        self.commits += 1
        if self.fail_after:
            self.fail_after = False
            raise GatewayUnavailable()
        return json_copy(result)

    async def cas_append_guarded(self, *args, guards, **kwargs):
        if guards:
            raise AssertionError("preview-only test store has no formal authority guards")
        return await self.cas_append(*args, **kwargs)


class HTTPTestAuth:
    async def me(self, access):
        identity = {"one": 41, "two": 42}.get(access)
        if identity is None:
            from src.cloudbase.auth import AuthError
            raise AuthError("AUTH_SESSION_INVALID")
        return {"id": identity, "username": f"tester_{identity}", "is_active": True, "is_admin": False}


@unittest.skipUnless(PACKAGE_FILE, "explicit exact original package input required")
class PlayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        raw = Path(PACKAGE_FILE).read_bytes()
        cls.package = parse_package_json(raw)
        if content_hash(cls.package) != ORIGINAL_PACKAGE_HASH:
            raise RuntimeError("CLOUDBASE_TEST_PACKAGE_HASH_MISMATCH")
        cls.manifest = original_preview(cls.package)

    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("offline play test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.gateway = DurableTestGateway(self.manifest)
        self.service = CloudBasePlay(self.gateway, [self.manifest])

    def equal_private(self, actual, expected, label="private projection"):
        self.assertEqual(content_hash(actual), content_hash(expected), label + " digest mismatch")

    async def start(self, owner=41, role=None, prefix="start"):
        role = role or self.package["characters"][0]["id"]
        opening = await self.service.run(owner, "opening.create", {
            "release_id": 1, "character_id": role, "idempotency_key": prefix + "-open"})
        view = await self.service.run(owner, "play.create", {
            "opening_session_id": opening["session_id"], "idempotency_key": prefix + "-play"})
        return opening, view

    @staticmethod
    def next_command(view, key):
        actions = view.get("mechanics", {}).get("available_actions", [])
        command = {"expected_revision": view["revision"], "idempotency_key": key}
        if actions:
            return {**command, "action": "PERFORM_ACTION", "target": {"action_id": actions[0]["id"]}}
        if view["can_advance"]:
            return {**command, "action": "ADVANCE_PHASE"}
        if view["phase_complete"] and view["mechanics"]["can_finish_phase"]:
            return {**command, "action": "SETTLE"}
        raise AssertionError("private case has no legal route to next phase")

    async def test_all_four_roles_complete_after_json_restoration_and_refresh(self):
        self.assertEqual(len(self.package["characters"]), 4)
        for index, character in enumerate(self.package["characters"]):
            with self.subTest(role_number=index):
                owner = 100 + index
                opening, view = await self.start(owner, character["id"], prefix=f"role-{index}")
                self.assertEqual(view["selected_character_id"], character["id"])
                self.assertIsNone(view["settlement"])
                phases, investigated = set(), 0
                for step in range(40):
                    phases.add(view["current_phase"]["id"])
                    if view["settled"]:
                        break
                    command = self.next_command(view, f"role-{index}-command-{step}")
                    investigated += command["action"] == "PERFORM_ACTION"
                    view = await self.service.run(owner, "play.act", {"play_id": view["play_id"], "command": command})
                    # New service object; every operation hydrates from a real
                    # JSON round-trip, not a retained SQLAlchemy Session.
                    self.service = CloudBasePlay(self.gateway, [self.manifest])
                    refreshed = await self.service.run(owner, "play.get", {"play_id": view["play_id"]})
                    self.equal_private(view, refreshed, "refreshed play")
                    if not view["settled"]:
                        self.assertIsNone(view["settlement"])
                self.assertTrue(view["settled"], "did not reach legal settlement")
                self.assertIsNotNone(view["settlement"])
                self.assertGreater(investigated, 0)
                self.assertEqual(len(phases), len(self.package["phases"]))
                self.equal_private(await self.service.run(owner, "opening.get", {"session_id": opening["session_id"]}), opening)
                found = await self.service.run(owner, "play.find", {"opening_session_id": opening["session_id"]})
                self.equal_private(found, view)
                library = await self.service.run(owner, "play.library", {"offset": 0, "limit": 20})
                self.assertEqual(len(library["items"]), 1)
                self.assertFalse(library["has_more"])
                self.assertIsNone(view["model"].get("provider"))
                self.assertFalse(view["public_preview"]["ai_dialogue_available"])

    async def test_cross_owner_get_find_and_workspace_injection_are_rejected(self):
        opening, view = await self.start()
        with self.assertRaises(PackageRuntimeError):
            await self.service.run(42, "opening.get", {"session_id": opening["session_id"]})
        with self.assertRaises(PackagePlayError):
            await self.service.run(42, "play.get", {"play_id": view["play_id"]})
        with self.assertRaises(PackagePlayError):
            await self.service.run(42, "play.find", {"opening_session_id": opening["session_id"]})
        snapshot = copy.deepcopy(self.gateway.records[("play_workspace", "cb-user:41", "cb-user:41")]["payload"])
        with self.assertRaisesRegex(ValueError, "CLOUDBASE_WORKSPACE_INVALID"):
            Workspace(42, snapshot, [self.manifest])
        snapshot["owner"] = 42
        with self.assertRaisesRegex(ValueError, "CLOUDBASE_WORKSPACE_OWNER_INVALID"):
            Workspace(42, snapshot, [self.manifest])

    async def test_tampered_event_hash_and_binding_are_not_returned_as_success(self):
        _, view = await self.start()
        command = self.next_command(view, "tamper-action")
        await self.service.run(41, "play.act", {"play_id": view["play_id"], "command": command})
        record_key = ("play_workspace", "cb-user:41", "cb-user:41")
        original = copy.deepcopy(self.gateway.records[record_key])
        for collection, field in (("events", "event_hash"), ("plays", "binding_hash"), ("openings", "binding_hash")):
            self.gateway.records[record_key] = copy.deepcopy(original)
            self.gateway.records[record_key]["payload"][collection][0][field] = "0" * 64
            with self.subTest(collection=collection), self.assertRaises((PackagePlayError, PackageRuntimeError)):
                await self.service.run(41, "play.get", {"play_id": view["play_id"]})

    async def test_repeat_command_returns_original_receipt_after_later_writes(self):
        _, view = await self.start()
        first_body = {"play_id": view["play_id"], "command": self.next_command(view, "receipt-first")}
        first = await self.service.run(41, "play.act", first_body)
        later = await self.service.run(41, "play.act", {
            "play_id": view["play_id"], "command": self.next_command(first, "receipt-later")})
        self.assertGreater(later["revision"], first["revision"])
        commits = self.gateway.commits
        repeated = await self.service.run(41, "play.act", first_body)
        self.equal_private(repeated, first, "idempotent original response")
        self.assertEqual(self.gateway.commits, commits)
        current = await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.equal_private(current, later, "later state remains")

    async def test_parallel_commands_have_one_cas_winner(self):
        _, view = await self.start()
        self.gateway.yield_before_cas = True
        bodies = [{"play_id": view["play_id"], "command": self.next_command(view, f"race-{index}")} for index in range(2)]
        commits = self.gateway.commits
        results = await asyncio.gather(*(self.service.run(41, "play.act", body) for body in bodies), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, GatewayConflict) for result in results), 1)
        self.assertEqual(self.gateway.commits, commits + 1)
        current = await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.assertEqual(current["revision"], view["revision"] + 1)

    async def test_failed_and_unknown_writes_do_not_report_success_or_retry(self):
        _, view = await self.start()
        body = {"play_id": view["play_id"], "command": self.next_command(view, "write-failure")}
        self.gateway.fail_before = True
        commits = self.gateway.commits
        with self.assertRaises(GatewayUnavailable):
            await self.service.run(41, "play.act", body)
        self.assertEqual(self.gateway.commits, commits)
        self.equal_private(await self.service.run(41, "play.get", {"play_id": view["play_id"]}), view)
        self.gateway.fail_after = True
        with self.assertRaises(GatewayUnavailable):
            await self.service.run(41, "play.act", body)
        self.assertEqual(self.gateway.commits, commits + 1)
        current = await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        recovered = await self.service.run(41, "play.act", body)
        self.equal_private(recovered, current, "uncertain write original receipt")
        self.assertEqual(self.gateway.commits, commits + 1)

    async def test_missing_record_after_initialization_conflict_never_reports_success(self):
        with patch.object(self.gateway, "get", AsyncMock(return_value=None)), patch.object(
                self.gateway, "create", AsyncMock(side_effect=GatewayConflict("RECORD_EXISTS"))):
            with self.assertRaises(GatewayConflict) as caught:
                await self.service.run(41, "opening.create", {"release_id": 1,
                    "character_id": self.package["characters"][0]["id"], "idempotency_key": "missing-init"})
        self.assertEqual(caught.exception.code, "CLOUDBASE_WORKSPACE_NOT_READY")
        self.assertEqual(self.gateway.commits, 0)

    async def test_workspace_never_reads_legacy_model_environment(self):
        with patch("src.fusion.agents.PlayerModelSettings.from_env", side_effect=AssertionError("legacy env forbidden")), patch("os.getenv", side_effect=AssertionError("model env forbidden")):
            unit = Workspace(41, None, [self.manifest])
            try:
                self.assertFalse(unit.play.model.available)
                self.assertFalse(unit.play.model.settings.paid_calls_enabled)
                self.assertEqual(unit.play.model.settings.retries, 0)
                self.assertNotIn("script_package_releases", unit.metadata.tables)
                self.assertNotIn("users", unit.metadata.tables)
            finally:
                unit.close()

    async def test_http_real_rules_health_refresh_and_conflict_envelopes(self):
        with patch.dict("os.environ", {"CORS_ORIGINS": ""}):
            app = create_app(gateway=self.gateway, auth=HTTPTestAuth(), manifests=[self.manifest])
        headers = {"Authorization": "Bearer one"}
        with TestClient(app, client=("127.0.0.1", 5555)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            catalog = client.get("/api/fusion/package-releases", headers=headers)
            self.assertEqual(catalog.status_code, 200)
            self.assertEqual(len(catalog.json()["data"]), 1)
            response = client.post("/api/fusion/package-sessions", headers=headers, json={
                "release_id": 1, "character_id": self.package["characters"][0]["id"], "idempotency_key": "http-open"})
            self.assertEqual(response.status_code, 201)
            opening = response.json()["data"]
            response = client.post("/api/fusion/package-plays", headers=headers, json={
                "opening_session_id": opening["session_id"], "idempotency_key": "http-play"})
            self.assertEqual(response.status_code, 201)
            view = response.json()["data"]
            action_url = f"/api/fusion/package-plays/{view['play_id']}/actions"
            command = self.next_command(view, "http-action")
            response = client.post(action_url, headers=headers, json=command)
            self.assertEqual(response.status_code, 200)
            saved = response.json()["data"]
            refreshed = client.get(f"/api/fusion/package-plays/{view['play_id']}", headers=headers)
            self.assertEqual(refreshed.status_code, 200)
            self.equal_private(refreshed.json()["data"], saved, "HTTP persisted refresh")
            conflict = client.post(action_url, headers=headers, json={**command, "idempotency_key": "http-conflict"})
            self.assertEqual(conflict.status_code, 409)
            self.assertNotIn("data", conflict.json())
            self.assertEqual(conflict.headers["cache-control"], "no-store")
            foreign = client.get(f"/api/fusion/package-plays/{view['play_id']}", headers={"Authorization": "Bearer two"})
            self.assertEqual(foreign.status_code, 404)
            self.assertNotIn("data", foreign.json())
            next_command = self.next_command(saved, "http-failed-write")
            commits = self.gateway.commits
            self.gateway.fail_before = True
            failed = client.post(action_url, headers=headers, json=next_command)
            self.assertEqual(failed.status_code, 503)
            self.assertNotIn("data", failed.json())
            self.assertEqual(self.gateway.commits, commits)
            self.gateway.fail_after = True
            uncertain = client.post(action_url, headers=headers, json=next_command)
            self.assertEqual(uncertain.status_code, 503)
            self.assertNotIn("data", uncertain.json())
            self.assertEqual(self.gateway.commits, commits + 1)
            recovered = client.post(action_url, headers=headers, json=next_command)
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(self.gateway.commits, commits + 1)


if __name__ == "__main__":
    unittest.main()
