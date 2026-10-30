"""Synthetic publication-authority seam tests; never an actual approval claim.

The authority stub stands in for CloudBaseAdmin's independently tested ORIGINAL
publication verifier. These tests validate ID/binding, historical scopes and
atomic dependency use; they do not fabricate production approvals or call LLMs.
"""
from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.cloudbase.app import create_app
from src.cloudbase.gateway import GatewayConflict, GatewayError, payload_digest
from src.cloudbase.play import CloudBasePlay, FORMAL_ID_OFFSET, ManifestReader, published_runtime
from src.fusion.package_play import PackagePlayError
from src.fusion.package_runtime import PackageRuntimeError
from src.fusion.package_validation import content_hash
from tests.cloudbase.test_admin import package as synthetic_package
from tests.cloudbase.test_play import DurableTestGateway, HTTPTestAuth


def formal_fixture():
    package = synthetic_package()
    return {"id": 1, "version_id": 1, "release_hash": "a" * 64, "package_hash": content_hash(package),
            "approval_id": 1, "approval_hash": "b" * 64, "basis_hash": "c" * 64,
            "bundle_hash": "d" * 64, "source_report_hash": "e" * 64,
            "submitted_by": 7, "created_at": "2026-10-30T00:00:00Z", "package": package,
            "current_approval_valid": True}


class Authority:
    def __init__(self):
        self.release = formal_fixture()
        self.version = 1
        self.revoked = False
        self.calls = []
        self.change_after_history = False

    def dependency(self):
        return {"kind": "admin_workspace", "key": "global", "owner": "system", "version": self.version,
                "digest": payload_digest({"version": self.version, "revoked": self.revoked})}

    def revoke(self):
        self.revoked = True
        self.version += 1

    async def read_runtime_manifests(self, *, require_current=True, release_ids=None):
        self.calls.append((require_current, copy.deepcopy(release_ids)))
        dependency = self.dependency()
        selected = release_ids is None or 1 in release_ids
        manifests = [copy.deepcopy(self.release)] if selected and not (require_current and self.revoked) else []
        for release in manifests:
            release["current_approval_valid"] = True if require_current else None
        if not require_current and self.change_after_history:
            self.change_after_history = False
            self.revoke()
        return {"manifests": manifests, "dependency": dependency}


class GuardedGateway(DurableTestGateway):
    def __init__(self, authority):
        super().__init__({})
        self.authority = authority
        self.guards = []
        self.before_guard = None

    async def cas_append_guarded(self, kind, key, owner, expected_version, payload, event, *,
                                 idempotency_key, request_digest, guards):
        self.guards.append(copy.deepcopy(guards))
        # The production SQL checks a committed receipt before its dependency
        # fence, so a revoked release can still retrieve its OLD response.
        if (kind, key, owner, idempotency_key) not in self.receipts:
            if self.before_guard is not None:
                callback, self.before_guard = self.before_guard, None
                callback()
            expected = self.authority.dependency()
            if guards != [{"kind": "admin_workspace", "key": "global", "owner": "system",
                           "expected_version": expected["version"], "digest": expected["digest"]}]:
                raise GatewayConflict("DEPENDENCY_CONFLICT")
        return await self.cas_append(kind, key, owner, expected_version, payload, event,
                                     idempotency_key=idempotency_key, request_digest=request_digest)


class FormalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("offline formal test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.authority = Authority()
        self.gateway = GuardedGateway(self.authority)
        self.service = CloudBasePlay(self.gateway, [], authority=self.authority)

    async def opening(self, key="formal-opening"):
        return await self.service.run(41, "opening.create", {
            "release_id": FORMAL_ID_OFFSET + 1, "character_id": "a", "idempotency_key": key})

    async def start(self):
        opening = await self.opening()
        view = await self.service.run(41, "play.create", {
            "opening_session_id": opening["session_id"], "idempotency_key": "formal-play"})
        return opening, view

    def test_formal_namespace_and_immutable_reference_are_separate_from_preview(self):
        raw = self.authority.release
        mapped = published_runtime(raw)
        self.assertEqual(mapped["id"], 1_000_001)
        self.assertEqual(mapped["version_id"], 1_000_001)
        self.assertEqual(mapped["source_release_id"], 1)
        self.assertEqual(mapped["source_version_id"], 1)
        self.assertEqual(mapped["source_release_hash"], raw["release_hash"])
        self.assertEqual(mapped["schema_version"], "cloudbase-published-runtime/1.0")
        self.assertNotEqual(mapped["release_hash"], raw["release_hash"])
        self.assertEqual(published_runtime({**raw, "current_approval_valid": None}), mapped)
        with self.assertRaisesRegex(ValueError, "CLOUDBASE_MANIFEST_POLICY_UNSUPPORTED"):
            ManifestReader([mapped])
        with self.assertRaises(ValueError):
            published_runtime({**raw, "publication_mode": "LOCAL_TRIAL"})

    async def test_current_catalog_and_formal_full_rules_flow_use_guarded_dependency(self):
        with patch("src.cloudbase.play.Workspace", side_effect=AssertionError("public catalog cannot create an actor")):
            catalog = await self.service.catalog()
        self.assertEqual([row["id"] for row in catalog], [1_000_001])
        self.assertNotIn("public_preview", catalog[0])
        self.assertNotIn("package", catalog[0])
        _, view = await self.start()
        self.assertNotIn("public_preview", view)
        for action, key in (("ADVANCE_PHASE", "formal-advance"), ("SETTLE", "formal-settle")):
            view = await self.service.run(41, "play.act", {"play_id": view["play_id"], "command": {
                "action": action, "expected_revision": view["revision"], "idempotency_key": key}})
        self.assertTrue(view["settled"])
        self.assertTrue(self.gateway.guards)
        expected = self.authority.dependency()
        self.assertTrue(all(guard == [{"kind": "admin_workspace", "key": "global", "owner": "system",
            "expected_version": expected["version"], "digest": expected["digest"]}] for guard in self.gateway.guards))

    async def test_revocation_blocks_new_openings_plays_actions_but_preserves_history(self):
        opening, view = await self.start()
        spare = await self.opening("before-revoke-spare")
        body = {"play_id": view["play_id"], "command": {
            "action": "ADVANCE_PHASE", "expected_revision": 0, "idempotency_key": "before-revoke-action"}}
        original = await self.service.run(41, "play.act", body)
        self.authority.revoke()
        self.authority.calls.clear()
        current = await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.assertEqual(content_hash(current), content_hash(original))
        self.assertEqual(self.authority.calls, [(False, [1])])
        self.assertEqual(await self.service.run(41, "catalog", {}), [])
        with self.assertRaises(PackageRuntimeError):
            await self.opening("after-revoke-opening")
        with self.assertRaises(PackagePlayError):
            await self.service.run(41, "play.create", {"opening_session_id": spare["session_id"], "idempotency_key": "after-revoke-play"})
        with self.assertRaises(PackagePlayError):
            await self.service.run(41, "play.act", {"play_id": view["play_id"], "command": {
                "action": "SETTLE", "expected_revision": 1, "idempotency_key": "after-revoke-action"}})
        commits = self.gateway.commits
        repeated = await self.service.run(41, "play.act", body)
        self.assertEqual(content_hash(repeated), content_hash(original))
        self.assertEqual(self.gateway.commits, commits)
        restored = await self.service.run(41, "opening.get", {"session_id": opening["session_id"]})
        self.assertEqual(content_hash(restored), content_hash(opening))

    async def test_authority_change_between_read_and_commit_never_persists_new_action(self):
        _, view = await self.start()
        commits = self.gateway.commits
        self.gateway.before_guard = self.authority.revoke
        with self.assertRaises(GatewayConflict) as caught:
            await self.service.run(41, "play.act", {"play_id": view["play_id"], "command": {
                "action": "ADVANCE_PHASE", "expected_revision": 0, "idempotency_key": "revocation-race"}})
        self.assertEqual(caught.exception.code, "DEPENDENCY_CONFLICT")
        self.assertEqual(self.gateway.commits, commits)
        current = await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.assertEqual(current["revision"], 0)

    async def test_two_authority_reads_must_share_one_snapshot_basis(self):
        _, view = await self.start()
        commits = self.gateway.commits
        self.authority.change_after_history = True
        with self.assertRaises(GatewayConflict) as caught:
            await self.service.run(41, "play.act", {"play_id": view["play_id"], "command": {
                "action": "ADVANCE_PHASE", "expected_revision": 0, "idempotency_key": "basis-race"}})
        self.assertEqual(caught.exception.code, "CLOUDBASE_PUBLICATION_AUTHORITY_CHANGED")
        self.assertEqual(self.gateway.commits, commits)

    async def test_changed_origin_hash_or_unscoped_history_cannot_rebind_existing_play(self):
        _, view = await self.start()
        self.authority.release["release_hash"] = "f" * 64
        with self.assertRaises(PackagePlayError):
            await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.authority.release["release_hash"] = "a" * 64
        with patch.object(self.authority, "read_runtime_manifests") as read:
            read.return_value = {"manifests": [], "dependency": self.authority.dependency()}
            with self.assertRaises(GatewayError) as caught:
                await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.assertEqual(caught.exception.code, "CLOUDBASE_FORMAL_HISTORY_SCOPE_INVALID")

    async def test_formal_requires_server_authority_and_rejects_local_trial(self):
        _, view = await self.start()
        self.service.authority = None
        with self.assertRaises(PackagePlayError):
            await self.service.run(41, "play.get", {"play_id": view["play_id"]})
        self.service.authority = self.authority
        self.authority.release["publication_mode"] = "LOCAL_TRIAL"
        with self.assertRaises(GatewayError):
            await self.service.run(41, "catalog", {})

    async def test_http_adapter_uses_live_injected_authority_and_keeps_manifest_private(self):
        with patch.dict(os.environ, {"CORS_ORIGINS": ""}):
            app = create_app(gateway=self.gateway, auth=HTTPTestAuth(), manifests=[])
        app.state.cloudbase_admin = self.authority
        headers = {"Authorization": "Bearer one"}
        with TestClient(app, client=("127.0.0.1", 5555)) as client:
            catalog = client.get("/api/fusion/package-releases")
            self.assertEqual(catalog.status_code, 200)
            self.assertEqual([row["id"] for row in catalog.json()["data"]], [1_000_001])
            self.assertNotIn("package", catalog.json()["data"][0])
            self.authority.revoke()
            response = client.get("/api/fusion/package-releases", headers=headers)
            self.assertEqual(response.json()["data"], [])
            response = client.post("/api/fusion/package-sessions", headers=headers, json={
                "release_id": 1_000_001, "character_id": "a", "idempotency_key": "http-revoked"})
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("data", response.json())


if __name__ == "__main__":
    unittest.main()
