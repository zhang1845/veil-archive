"""Offline HTTP admin paths with real domain adapter, isolated CAS/object seams."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.cloudbase.admin import CloudBaseAdmin
from src.cloudbase.app import create_app
from src.cloudbase.gateway import GatewayError, GatewayUnavailable
from src.fusion.source_bundles import SourceBundleStore
from test_admin import ACTOR, SOURCE, MemoryBlobs, MemoryGateway, package


ACCESS = "va_" + "a" * 64
HEADERS = {"Authorization": "Bearer " + ACCESS}
JOB_ID = "6b1e155e-1111-4111-8111-222222222222"


class RouteAuth:
    def __init__(self):
        self.user = copy.deepcopy(ACTOR)

    async def me(self, token):
        if token != ACCESS:
            raise GatewayError("AUTH_SESSION_INVALID", 401)
        return self.user


class JobGateway(MemoryGateway):
    def __init__(self):
        super().__init__()
        self.jobs = []
        self.rpc_calls = []
        self.rpc_error = None
        self.cancel_receipts = {}

    async def rpc(self, name, params):
        self.rpc_calls.append((name, copy.deepcopy(params)))
        if self.rpc_error:
            raise self.rpc_error
        if name == "veil_archive_job_list":
            return {"ok": True, "jobs": [copy.deepcopy(job) for job in self.jobs if job["kind"] == params["p_kind"]]}
        if name == "veil_archive_job_get":
            return {"ok": True, "job": next((copy.deepcopy(job) for job in self.jobs if job["id"] == params["p_job_id"]), None)}
        if name == "veil_archive_job_cancel":
            if params["p_idempotency_key"] in self.cancel_receipts:
                return {**copy.deepcopy(self.cancel_receipts[params["p_idempotency_key"]]), "replayed": True}
            job = next(job for job in self.jobs if job["id"] == params["p_job_id"])
            assert params["p_admin_actor_id"] == ACTOR["id"]
            assert params["p_owner_actor_id"] == job["actor_id"]
            assert params["p_expected_revision"] == job["revision"]
            assert params["p_kind"] == "creative"
            job.update(state="CANCELLED", revision=job["revision"] + 1)
            result = {"ok": True, "job": copy.deepcopy(job), "replayed": False}
            self.cancel_receipts[params["p_idempotency_key"]] = copy.deepcopy(result)
            return result
        raise AssertionError("Unexpected RPC; test must never create a model job")


class AdminRouteTests(unittest.TestCase):
    def setUp(self):
        self.gateway, self.auth, self.blobs = JobGateway(), RouteAuth(), MemoryBlobs()
        self.env = patch.dict("os.environ", {"CORS_ORIGINS": "https://static.example.test", "CLOUDBASE_PRIVATE_BUCKET": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch("socket.socket.connect", side_effect=AssertionError("offline HTTP admin test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.app = create_app(gateway=self.gateway, auth=self.auth, manifests=[])
        self.admin = CloudBaseAdmin(self.gateway, self.blobs)
        self.app.state.cloudbase_admin = self.admin
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def private(self, result, status):
        self.assertEqual(result.status_code, status, result.text)
        self.assertIn("no-store", result.headers.get("Cache-Control", ""))

    def test_every_existing_ui_path_denies_missing_nonadmin_and_inactive_before_pg(self):
        routes = [("GET", "/creative-jobs"), ("GET", "/authoring-jobs"), ("GET", "/review-candidates"),
                  ("GET", "/script-packages/1"), ("GET", "/script-packages/1/publication"),
                  ("POST", "/script-imports"), ("POST", "/script-packages/1/audits"),
                  ("POST", "/script-packages/1/approvals"), ("POST", "/script-packages/1/publish"),
                  ("POST", "/script-audits/1/dispositions"), ("POST", "/creative-jobs"),
                  ("POST", f"/creative-jobs/{JOB_ID}/cancel"), ("GET", "/source-bundles")]
        for changes, headers, status in [({}, {}, 401), ({"is_admin": False}, HEADERS, 403),
                                          ({"is_admin": "true"}, HEADERS, 403), ({"is_active": False}, HEADERS, 401)]:
            self.auth.user = {**ACTOR, **changes}
            for method, suffix in routes:
                with self.subTest(changes=changes, path=suffix):
                    result = self.client.request(method, "/api/admin/fusion" + suffix, headers=headers,
                                                 json={} if method == "POST" else None)
                    self.private(result, status)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.gateway.rpc_calls, [])

    def test_workbench_reads_real_empty_lists_and_actual_candidate_import(self):
        for path in ["creative-jobs", "authoring-jobs", "review-candidates", "source-bundles"]:
            result = self.client.get(f"/api/admin/fusion/{path}", headers=HEADERS)
            self.private(result, 200)
            self.assertEqual(result.json(), {"success": True, "data": []})
        self.assertEqual([call[1]["p_kind"] for call in self.gateway.rpc_calls], ["creative", "authoring"])
        submitted = self.client.post("/api/admin/fusion/script-imports", headers=HEADERS,
                                     json={"idempotency_key": "http-import", "package": package()})
        self.private(submitted, 200)
        imported = submitted.json()["data"]
        self.assertEqual(imported["status"], "SUCCEEDED")
        version = imported["version_id"]
        for path in [f"script-packages/{version}", f"script-packages/{version}/review",
                     f"script-packages/{version}/publication", f"script-imports/{imported['id']}"]:
            result = self.client.get("/api/admin/fusion/" + path, headers=HEADERS)
            self.private(result, 200)
            self.assertTrue(result.json()["success"])
        gate = self.client.get(f"/api/admin/fusion/script-packages/{version}/publication", headers=HEADERS).json()["data"]
        self.assertFalse(gate["can_approve"])
        self.assertFalse(gate["can_publish"])
        self.assertIsNone(gate["release"])

    def test_model_create_returns_unconfigured_and_never_enqueues(self):
        result = self.client.post("/api/admin/fusion/creative-jobs", headers=HEADERS,
            json={"idempotency_key": "original", "title": "虚构", "theme": "仅用于测试的主题", "player_count": 4,
                  "max_revision_rounds": 2, "cost_ceiling_cny": "0.50"})
        self.private(result, 503)
        self.assertEqual(result.json()["detail"], "CLOUDBASE_MODEL_DISABLED")
        self.assertEqual(self.gateway.rpc_calls, [])
        self.assertIsNone(self.gateway.record)

    def test_list_storage_failure_is_not_disguised_as_empty_success(self):
        self.gateway.rpc_error = GatewayUnavailable()
        result = self.client.get("/api/admin/fusion/creative-jobs", headers=HEADERS)
        self.private(result, 503)
        self.assertNotEqual(result.json(), {"success": True, "data": []})

    def test_creative_list_reads_actual_job_request_and_redacts_server_fields(self):
        self.gateway.jobs = [{"id": JOB_ID, "actor_id": ACTOR["id"], "kind": "creative", "state": "QUEUED",
            "revision": 0, "budget_cny": "0.50", "charged_cny": "0.00", "created_at": "2026-10-30T00:00:00Z",
            "updated_at": "2026-10-30T00:00:00Z", "lease_token": "NEVER_RETURN_LEASE",
            "request_payload": {"idempotency_key": "original", "title": "真实存储的测试标题", "theme": "仅用于测试的主题",
                "player_count": 4, "max_revision_rounds": 2, "cost_ceiling_cny": "0.50"},
            "payload": {"prepared_prompt": "NEVER_RETURN_PROMPT"}}]
        result = self.client.get("/api/admin/fusion/creative-jobs", headers=HEADERS)
        self.private(result, 200)
        job = result.json()["data"][0]
        self.assertEqual(job["request"]["title"], "真实存储的测试标题")
        self.assertEqual(job["state"], "QUEUED")
        self.assertEqual(job["charged_cost_cny"], "0.00")
        self.assertNotIn("NEVER_RETURN", result.text)
        self.assertEqual([call[0] for call in self.gateway.rpc_calls], ["veil_archive_job_list", "veil_archive_job_get"])
        cancelled = self.client.post(f"/api/admin/fusion/creative-jobs/{JOB_ID}/cancel", headers=HEADERS)
        self.private(cancelled, 200)
        self.assertEqual(cancelled.json()["data"]["state"], "CANCELLED")
        self.assertEqual(cancelled.json()["data"]["charged_cost_cny"], "0.00")
        self.assertNotIn("NEVER_RETURN", cancelled.text)
        repeated = self.client.post(f"/api/admin/fusion/creative-jobs/{JOB_ID}/cancel", headers=HEADERS)
        self.private(repeated, 200)
        self.assertEqual(repeated.json(), cancelled.json())
        cancelled_list = self.client.get("/api/admin/fusion/creative-jobs", headers=HEADERS)
        self.assertEqual(cancelled_list.json()["data"][0]["state"], "CANCELLED")
        params = next(params for name, params in self.gateway.rpc_calls if name == "veil_archive_job_cancel")
        self.assertEqual(params["p_admin_actor_id"], ACTOR["id"])
        self.assertEqual(params["p_kind"], "creative")

    def test_cancel_rejects_client_supplied_actor_kind_or_revision(self):
        result = self.client.post(f"/api/admin/fusion/creative-jobs/{JOB_ID}/cancel", headers=HEADERS,
                                  json={"actor_id": 999, "kind": "speech", "expected_revision": 0})
        self.private(result, 422)
        self.assertEqual(self.gateway.rpc_calls, [])

    def test_malformed_private_bodies_and_identifiers_have_fixed_safe_errors(self):
        for path, raw, expected in [
            ("script-imports", '{"package":"PRIVATE_SENTINEL","package":{}}', 422),
            ("script-packages/1/audits", '{"private":"PRIVATE_SENTINEL"', 422),
            ("script-packages/invalid/audits", '{}', 422),
            ("script-imports", '{"private":"' + "x" * 900001 + '"}', 413),
        ]:
            result = self.client.post("/api/admin/fusion/" + path,
                headers={**HEADERS, "Content-Type": "application/json"}, content=raw)
            self.private(result, expected)
            self.assertNotIn("PRIVATE_SENTINEL", result.text)
        self.assertIsNone(self.gateway.record)

    def test_bootstrap_and_player_raw_source_routes_are_absent(self):
        for path in ["/bootstrap", "/api/admin/fusion/_install_source", "/api/fusion/source-bundles/" + "a" * 64]:
            self.private(self.client.get(path, headers=HEADERS), 404)

    def test_raw_source_requires_admin_and_is_served_inert_with_no_store(self):
        async def install():
            with TemporaryDirectory(prefix="veil-admin-http-") as temporary:
                root = Path(temporary)
                (root / "input").mkdir()
                (root / "input/case.md").write_bytes(SOURCE)
                store = SourceBundleStore(root / "private")
                frozen = store.freeze(root / "input", {"schema_version": "source-plan/1.0", "script_key": "synthetic-case",
                    "edition": "synthetic-only", "notes": [], "sources": [
                        {"relative_path": "case.md", "kind": "original", "material_type": "host"}]})
                archive = await self.blobs.archive_source_bundle(store, frozen["bundle_hash"])
            await self.admin.register_source_archive(ACTOR, archive, idempotency_key="http-source-install")
            return archive
        archive = asyncio.run(install())
        source_id = next(iter(archive["file_refs"]))
        path = f"/api/admin/fusion/source-bundles/{archive['bundle_hash']}/sources/{source_id}"
        result = self.client.get(path, headers=HEADERS)
        self.private(result, 200)
        self.assertEqual(result.content, SOURCE)
        self.assertTrue(result.headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", result.headers["Content-Security-Policy"])
        self.auth.user = {**ACTOR, "is_admin": False}
        self.private(self.client.get(path, headers=HEADERS), 403)


if __name__ == "__main__":
    unittest.main()
