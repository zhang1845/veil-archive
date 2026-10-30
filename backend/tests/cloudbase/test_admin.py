"""Private synthetic sources + real domain services; no cloud, model, app startup.

PYTHONPATH=backend python -m unittest discover -s backend/tests/cloudbase -p 'test_admin.py'
"""
from __future__ import annotations

import copy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.cloudbase.admin import AdminError, AdminWorkspace, CloudBaseAdmin
from src.cloudbase.gateway import GatewayConflict, payload_digest
from src.cloudbase.storage import PrivateBlobStore, PrivateStorageError
from src.fusion.package_validation import canonical_json, content_hash
from src.fusion.source_bundles import SourceBundleStore
from src.db.models.script_publication import ScriptPackageRelease, ScriptPublicationApproval


ACTOR = {"id": 7, "username": "actual_admin", "is_admin": True, "is_active": True, "is_guest": False}
SOURCE = b"# synthetic-case\nThe fictional clock was under repair.\n"


def package():
    refs = [{"source_id": "original", "anchor": "synthetic-case"}]
    return {
        "schema_version": "script-package/1.1", "script_key": "synthetic-case", "content_version": "draft-1",
        "title": "虚构钟楼测试案", "player_count": 2,
        "sources": [{"id": "original", "relative_path": "case.md", "sha256": sha256(SOURCE).hexdigest(),
                     "kind": "original", "media_type": "text/markdown", "original_source_ids": []}],
        "introduction": {"text": "虚构钟楼的时钟停了。", "sources": copy.deepcopy(refs)},
        "characters": [{"id": key, "name": name, "sources": copy.deepcopy(refs)}
                       for key, name in [("a", "甲"), ("b", "乙")]],
        "initial_phase_id": "opening",
        "phases": [{"id": "opening", "title": "调查", "next_phase_id": "ending", "sources": copy.deepcopy(refs)},
                   {"id": "ending", "title": "结局", "next_phase_id": None, "sources": copy.deepcopy(refs)}],
        "knowledge": [{"id": f"memory-{key}", "text": "角色的虚构私本。", "kind": "FACT",
                       "visibility": "CHARACTER_PRIVATE", "character_id": key, "release": {"phase_id": "opening"},
                       "disclosure": "MAY_SHARE", "sources": copy.deepcopy(refs)} for key in ("a", "b")],
        "evidence": [{"id": "clock", "text": "维修记录。", "visibility": "PUBLIC", "character_id": None,
                      "release": {"phase_id": "opening"}, "disclosure": "PUBLIC", "sources": copy.deepcopy(refs)}],
        "truth": [{"id": "answer", "text": "虚构时钟正在维修。", "visibility": "SYSTEM_TRUTH", "sources": copy.deepcopy(refs)}],
        "settlement": {"phase_id": "ending", "truth_ids": ["answer"],
                       "instructions": {"text": "核对维修记录。", "sources": copy.deepcopy(refs)}},
    }


class MemoryGateway:
    """CAS transport seam, not an alternate domain or game implementation."""
    def __init__(self):
        self.record = None
        self.receipts = {}
        self.calls = []
        self.conflict_next = False

    async def get(self, kind, key, owner):
        self.calls.append("get")
        assert (kind, key, owner) == ("admin_workspace", "global", "system")
        return copy.deepcopy(self.record)

    async def create(self, kind, key, owner, payload, **kwargs):
        self.calls.append("create")
        if self.record is not None:
            raise GatewayConflict("RECORD_EXISTS")
        self.record = {"kind": kind, "key": key, "owner": owner, "version": 1,
                       "payload": copy.deepcopy(payload), "digest": payload_digest(payload)}
        return {"record": copy.deepcopy(self.record), "replayed": False, "event": None}

    async def cas_append(self, kind, key, owner, expected_version, payload, event, *, idempotency_key, request_digest):
        self.calls.append("cas")
        if idempotency_key in self.receipts:
            digest, original = self.receipts[idempotency_key]
            if digest != request_digest:
                raise GatewayConflict("IDEMPOTENCY_CONFLICT")
            return {**copy.deepcopy(original), "replayed": True}
        if self.conflict_next:
            self.conflict_next = False
            raise GatewayConflict("VERSION_CONFLICT")
        if self.record["version"] != expected_version:
            raise GatewayConflict("VERSION_CONFLICT")
        self.record = {"kind": kind, "key": key, "owner": owner, "version": expected_version + 1,
                       "payload": copy.deepcopy(payload), "digest": payload_digest(payload)}
        result = {"record": copy.deepcopy(self.record), "replayed": False, "event": copy.deepcopy(event)}
        self.receipts[idempotency_key] = (request_digest, copy.deepcopy(result))
        return result


class MemoryBlobs(PrivateBlobStore):
    """Only replace object transport; use real archive bridge and SourceBundleStore."""
    def __init__(self):
        super().__init__(None, "private-test")
        self.objects = {}

    async def put_immutable(self, data, *, kind="source", media_type="application/octet-stream", expected_sha256=None):
        digest = sha256(data).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise PrivateStorageError("PRIVATE_STORAGE_INTEGRITY_FAILED", 409)
        ref = self.reference(kind, digest, len(data), media_type)
        self.objects.setdefault(ref.key, data)
        await self.read_verified(ref)
        return ref

    async def read_verified(self, ref):
        self._validate_reference(ref)
        data = self.objects.get(ref.key)
        if data is None or len(data) != ref.size_bytes or sha256(data).hexdigest() != ref.sha256:
            raise PrivateStorageError("PRIVATE_STORAGE_INTEGRITY_FAILED", 409)
        return data


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway, self.blobs = MemoryGateway(), MemoryBlobs()
        self.admin = CloudBaseAdmin(self.gateway, self.blobs)
        self.document = package()

    async def import_candidate(self, key="import-1", document=None):
        return await self.admin.run(ACTOR, "candidate.import", {"idempotency_key": key, "package": document or self.document})

    async def install_sources(self):
        with TemporaryDirectory(prefix="veil-admin-test-") as temporary:
            root = Path(temporary)
            source = root / "input"
            source.mkdir()
            (source / "case.md").write_bytes(SOURCE)
            store = SourceBundleStore(root / "private")
            plan = {"schema_version": "source-plan/1.0", "script_key": "synthetic-case", "edition": "synthetic-only",
                    "notes": [], "sources": [{"relative_path": "case.md", "kind": "original", "material_type": "host"}]}
            frozen = store.freeze(source, plan)
            archive = await self.blobs.archive_source_bundle(store, frozen["bundle_hash"])
        await self.admin.register_source_archive(ACTOR, archive, idempotency_key="install-synthetic-source")
        return frozen["bundle_hash"], archive

    def audit_request(self, bundle, *, warning=False):
        return {"idempotency_key": "manual-audit-1", "expected_package_hash": content_hash(self.document),
                "bundle_hash": bundle, "report": {"schema_version": "script-audit/1.0", "summary": "管理员实际人工记录（合成测试）。",
                "coverage": ["PROVENANCE", "TIMELINE", "EVIDENCE", "KNOWLEDGE_BOUNDARY", "PLAYABILITY"],
                "findings": [{"id": "warning-one", "category": "PROVENANCE", "severity": "WARNING",
                              "target": {"collection": "introduction", "id": None}, "message": "需人工核对虚构来源。",
                              "sources": copy.deepcopy(self.document["introduction"]["sources"])}] if warning else []}}

    async def test_require_fresh_active_non_guest_admin_before_any_gateway_access(self):
        for changes in [{"is_admin": False}, {"is_admin": "true"}, {"is_active": False}, {"is_guest": True}, {"id": True}, {"id": 0}]:
            with self.assertRaises(AdminError) as caught:
                await self.admin.run({**ACTOR, **changes}, "candidate.list")
            self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.gateway.calls, [])

    async def test_empty_candidates_are_real_read_only_state(self):
        self.assertEqual(await self.admin.run(ACTOR, "candidate.list"), [])
        self.assertEqual(await self.admin.run(ACTOR, "source.list"), [])
        self.assertIsNone(self.gateway.record)

    async def test_import_get_check_reload_and_no_formal_or_preview_release(self):
        imported = await self.import_candidate()
        self.assertEqual(imported["status"], "SUCCEEDED")
        self.assertFalse(imported["publication_ready"])
        fresh = CloudBaseAdmin(self.gateway, self.blobs)
        version = await fresh.run(ACTOR, "candidate.get", {"version_id": imported["version_id"]})
        self.assertEqual(version["package"], self.document)
        checked = await fresh.run(ACTOR, "candidate.check", {"version_id": imported["version_id"]})
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["publication_ready"])
        candidates = await fresh.run(ACTOR, "candidate.list")
        self.assertEqual(candidates[0]["title"], self.document["title"])
        stored = self.gateway.record["payload"]
        self.assertEqual(set(stored["rows"]), set(AdminWorkspace.empty()["rows"]))
        self.assertEqual(stored["rows"]["releases"], [])
        self.assertNotIn("hashed_password", json.dumps(stored))
        self.assertNotIn("users", stored["rows"])

    async def test_blocked_import_persists_actual_validation_report(self):
        invalid = {**self.document, "player_count": 99}
        result = await self.import_candidate(document=invalid)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIsNone(result["version_id"])
        self.assertFalse(result["report"]["valid"])
        self.assertEqual(await self.admin.run(ACTOR, "candidate.list"), [])

    async def test_changed_import_key_conflicts_and_identical_replay_keeps_original_response(self):
        first = await self.import_candidate()
        next_document = {**self.document, "content_version": "draft-2"}
        await self.import_candidate("import-2", next_document)
        revision = self.gateway.record["version"]
        self.assertEqual(await self.import_candidate(), first)
        self.assertEqual(self.gateway.record["version"], revision)
        with self.assertRaises(AdminError) as caught:
            await self.import_candidate(document=next_document)
        self.assertEqual(caught.exception.status, 409)

    async def test_cas_conflict_never_returns_uncommitted_success_or_retries(self):
        await self.import_candidate()
        before = copy.deepcopy(self.gateway.record)
        calls = self.gateway.calls.count("cas")
        self.gateway.conflict_next = True
        with self.assertRaises(GatewayConflict):
            await self.import_candidate("other", {**self.document, "content_version": "draft-other"})
        self.assertEqual(self.gateway.record, before)
        self.assertEqual(self.gateway.calls.count("cas"), calls + 1)

    async def test_no_model_jobs_are_manufactured_and_unknown_endpoints_fail(self):
        for operation in ["creative.create", "authoring.create", "creative.step", "authoring.step"]:
            with self.assertRaises(AdminError) as caught:
                await self.admin.run(ACTOR, operation, {"title": "irrelevant"})
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "CLOUDBASE_MODEL_DISABLED"))
        with self.assertRaises(AdminError) as caught:
            await self.admin.run(ACTOR, "delete-everything")
        self.assertEqual(caught.exception.status, 501)
        self.assertEqual(self.gateway.calls, [])

    async def test_source_registration_is_private_and_rejects_injected_reports(self):
        with self.assertRaises(AdminError):
            await self.admin.run(ACTOR, "_install_source", {})
        with self.assertRaises(AdminError):
            await self.admin.register_source_archive(ACTOR, {"bundle_hash": "a" * 64, "manifest_ref": {},
                "file_refs": {}, "report_refs": {"b" * 64: {}}}, idempotency_key="inject-report")
        self.assertIsNone(self.gateway.record)

    async def test_private_sources_verify_manual_review_and_warning_disposition_survive_reload(self):
        imported = await self.import_candidate()
        bundle, archive = await self.install_sources()
        body = self.audit_request(bundle, warning=True)
        audit = await self.admin.run(ACTOR, "manual_audit.create", {"version_id": imported["version_id"], "request": body})
        self.assertEqual(audit["open_warnings"], 1)
        refs = self.gateway.record["payload"]["source_archives"][bundle]["report_refs"]
        self.assertIn(audit["source_report_hash"], refs)
        self.assertIn(refs[audit["source_report_hash"]]["key"], self.blobs.objects)
        disposed = await CloudBaseAdmin(self.gateway, self.blobs).run(ACTOR, "finding.dispose", {
            "audit_id": audit["id"], "request": {"idempotency_key": "dispose-warning",
            "expected_package_hash": audit["package_hash"], "expected_audit_hash": audit["audit_hash"],
            "expected_revision": 0, "finding_id": "warning-one", "status": "ACKNOWLEDGED", "note": "已人工核对。"}})
        self.assertEqual(disposed["open_warnings"], 0)
        review = await self.admin.run(ACTOR, "review.get", {"version_id": imported["version_id"]})
        self.assertEqual(review["audits"][0], disposed)
        # Re-running a trusted bootstrap cannot erase reports or replace bytes.
        await self.admin.register_source_archive(ACTOR, archive, idempotency_key="install-synthetic-source")
        self.assertIn(audit["source_report_hash"], self.gateway.record["payload"]["source_archives"][bundle]["report_refs"])

    async def test_complete_manual_review_cannot_approve_or_publish_without_model_audit(self):
        imported = await self.import_candidate()
        bundle, _ = await self.install_sources()
        await self.admin.run(ACTOR, "manual_audit.create", {"version_id": imported["version_id"], "request": self.audit_request(bundle)})
        gate = await self.admin.run(ACTOR, "publication.get", {"version_id": imported["version_id"]})
        checks = {item["code"]: item["passed"] for item in gate["checks"]}
        self.assertTrue(checks["MANUAL_AUDIT_COMPLETE"])
        self.assertTrue(checks["SOURCE_VERIFIED"])
        self.assertFalse(checks["MODEL_AUDIT_COMPLETE"])
        self.assertFalse(gate["can_approve"])
        self.assertFalse(gate["can_publish"])
        with self.assertRaises(AdminError) as caught:
            await self.admin.run(ACTOR, "publication.approve", {"version_id": imported["version_id"], "request": {
                "idempotency_key": "human-confirm", "expected_package_hash": content_hash(self.document),
                "bundle_hash": bundle, "expected_basis_hash": gate["basis_hash"], "model_dispositions": [], "note": "不能省略模型审核。"}})
        self.assertEqual(caught.exception.code, "PUBLICATION_GATE_BLOCKED")
        with self.assertRaises(AdminError):
            await self.admin.run(ACTOR, "publication.publish", {"version_id": imported["version_id"], "request": {
                "idempotency_key": "publish", "approval_id": 1, "expected_approval_hash": "0" * 64, "expected_basis_hash": gate["basis_hash"]}})
        self.assertEqual(self.gateway.record["payload"]["rows"]["approvals"], [])
        self.assertEqual(self.gateway.record["payload"]["rows"]["releases"], [])

    async def test_missing_source_storage_blocks_review_without_recording_success(self):
        imported = await self.import_candidate()
        with self.assertRaises(AdminError) as caught:
            await self.admin.run(ACTOR, "manual_audit.create", {"version_id": imported["version_id"], "request": self.audit_request("a" * 64)})
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.gateway.record["payload"]["rows"]["audits"], [])
        gate = await self.admin.run(ACTOR, "publication.get", {"version_id": imported["version_id"]})
        self.assertFalse(gate["can_approve"])

    async def test_changed_private_object_blocks_audit_and_preserves_pg_state(self):
        imported = await self.import_candidate()
        bundle, archive = await self.install_sources()
        file_ref = next(iter(archive["file_refs"].values()))
        self.blobs.objects[file_ref["key"]] = b"corrupted-source"
        before = copy.deepcopy(self.gateway.record)
        with self.assertRaises(AdminError) as caught:
            await self.admin.run(ACTOR, "manual_audit.create", {"version_id": imported["version_id"], "request": self.audit_request(bundle)})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.gateway.record, before)

    async def test_source_read_returns_bytes_only_after_admin_authorization(self):
        bundle, archive = await self.install_sources()
        source_id = next(iter(archive["file_refs"]))
        result = await self.admin.run(ACTOR, "source.read", {"bundle_hash": bundle, "source_id": source_id})
        self.assertEqual(result, (SOURCE, "text/markdown"))
        self.assertEqual(len(await self.admin.run(ACTOR, "source.list")), 1)

    async def test_tampered_workspace_or_candidate_never_reads_as_valid(self):
        await self.import_candidate()
        self.gateway.record["payload"]["rows"]["versions"][0]["package_json"] = canonical_json({"fake": "PRIVATE_SENTINEL"})
        with self.assertRaises(AdminError) as caught:
            await self.admin.run(ACTOR, "candidate.list")
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        self.gateway.record["payload"]["rows"]["users"] = []
        with self.assertRaises(AdminError) as caught:
            await self.admin.run(ACTOR, "candidate.list")
        self.assertEqual(caught.exception.code, "CLOUDBASE_ADMIN_WORKSPACE_INVALID")

    async def test_internal_runtime_reader_keeps_candidates_out_and_returns_pg_dependency(self):
        self.assertEqual(await self.admin.read_runtime_manifests(), {"manifests": [], "dependency": None})
        await self.import_candidate()
        before = copy.deepcopy(self.gateway.record)
        for current in (True, False):
            result = await self.admin.read_runtime_manifests(require_current=current)
            self.assertEqual(result["manifests"], [])
            self.assertEqual(result["dependency"], {"kind": "admin_workspace", "key": "global", "owner": "system",
                "version": before["version"], "digest": before["digest"]})
        self.assertEqual(self.gateway.record, before)
        with self.assertRaises(AdminError) as caught:
            await self.admin.read_runtime_manifests(release_ids=[1])
        self.assertEqual(caught.exception.status, 404)

    async def test_internal_runtime_reader_rejects_corrupt_formal_history_even_when_historical(self):
        imported = await self.import_candidate()
        unit = AdminWorkspace(ACTOR, self.gateway.record["payload"])
        try:
            # Intentionally corrupt private persistence fixture. No approve or
            # publish method is bypassed to produce a usable release/manifest.
            common = {"version_id": imported["version_id"], "submitted_by": ACTOR["id"],
                "package_hash": content_hash(self.document), "bundle_hash": "a" * 64,
                "basis_hash": "b" * 64, "source_report_hash": "c" * 64}
            approval = ScriptPublicationApproval(**common, idempotency_key="corrupt-approval", input_hash="d" * 64,
                approval_hash="e" * 64, approval_json="{}")
            unit.db.add(approval)
            unit.db.flush()
            release = ScriptPackageRelease(**common, approval_id=approval.id, idempotency_key="corrupt-release",
                input_hash="f" * 64, release_hash="0" * 64, release_json="{}")
            unit.db.add(release)
            self.gateway.record["payload"] = unit.snapshot()
        finally:
            unit.close()
        for current in (True, False):
            with self.assertRaises(AdminError):
                await self.admin.read_runtime_manifests(require_current=current)


if __name__ == "__main__":
    unittest.main()
