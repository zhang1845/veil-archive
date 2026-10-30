"""Offline generation → real publication gates → role-bound play; no paid I/O."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.routes import creative_routes
from src.db.base import SQLAlchemyBase
from src.db.models import User
from src.fusion import creative_generation, authoring_model
from src.fusion.authoring_jobs import AuthoringJobStore
from src.fusion.creative_generation import CreativeError, CreativeRequest, CreativeStore, freeze_draft, run_creative_job
from src.fusion.package_play import PackagePlayService
from src.fusion.package_runtime import PackageRuntimeService
from src.fusion.package_validation import validate_package
from src.fusion.script_publication import ScriptPublicationService
from src.fusion.script_review import ScriptReviewService
from src.fusion.source_bundles import SourceBundleStore
from src.schemas.script_publication import ApprovePublicationRequest, PublishPackageRequest
from src.schemas.script_review import SubmitAuditRequest
from tests.fusion_security.creative_fixture import story, compiled_package
from tests.fusion_security.test_authoring_model import fixture_config, fixture_report, good_response, draft_for


@pytest.fixture
def case(tmp_path, monkeypatch):
    sources = SourceBundleStore(tmp_path / "sources")
    store = CreativeStore(tmp_path / "creative")
    engine = create_engine(f"sqlite:///{tmp_path / 'isolated.sqlite'}", connect_args={"check_same_thread": False, "autocommit": False})
    SQLAlchemyBase.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False)
    with factory.begin() as db:
        db.add_all([User(id=1, username="creative-admin", email="admin@example.invalid", hashed_password="NO_LOGIN", is_admin=True, is_active=True),
                    User(id=2, username="creative-player", email="player@example.invalid", hashed_password="NO_LOGIN", is_admin=False, is_active=True)])
    jobs = AuthoringJobStore(factory)
    request = CreativeRequest(idempotency_key="story", title="虚构小站测试", theme="雨夜小站的登记簿之谜，严格用于原创测试。", player_count=3)
    job = store.create(request, 1)
    draft = story()
    state = SimpleNamespace(store=store, sources=sources, jobs=jobs, request=request, job=job, draft=draft, factory=factory, package=None)
    async def respond(*args, **kwargs):
        messages = args[0]
        if "原创推理剧本作者" in messages[0].content:
            # The reservation must already be durable at first byte dispatch.
            saved = store.get(job["id"])
            assert saved["state"] == "GENERATING" and Decimal(saved["charged_cost_cny"]) > 0
            return good_response(draft.model_dump())
        current = store.get(job["id"])
        context = jobs.inputs(current["authoring_job_id"])["context"]
        state.package = compiled_package(context, draft)
        if "max_completion_tokens" in kwargs and kwargs["max_completion_tokens"] == 8192:
            return good_response(draft_for(state.package))
        return good_response(fixture_report(state.package))
    client = SimpleNamespace(chat_completion=AsyncMock(side_effect=respond))
    monkeypatch.setattr(creative_generation, "OpenAILLMService", lambda **kwargs: client)
    monkeypatch.setattr(authoring_model, "OpenAILLMService", lambda **kwargs: client)
    state.client = client
    yield state
    engine.dispose()


def run(case, paid=True):
    return asyncio.run(run_creative_job(case.job["id"], case.store, case.sources, case.jobs, fixture_config(), allow_paid=paid))


@pytest.mark.parametrize("patch", [{"player_count": 2}, {"player_count": True}, {"player_count": "4"}, {"theme": "x"}, {"title": " "}, {"publish": True}])
def test_request_rejects_invalid_or_privileged_fields(case, patch):
    with pytest.raises(ValidationError):
        CreativeRequest.model_validate(case.request.model_dump() | patch)


def test_queue_idempotent_conflicting_input_and_cancel(case):
    assert case.store.create(case.request, 1) == case.job
    assert len(case.store.list()) == 1 and not case.client.chat_completion.called
    with pytest.raises(CreativeError, match="CONFLICT"):
        case.store.create(case.request.model_copy(update={"title": "different"}), 1)
    case.store.cancel(case.job["id"])
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        run(case)
    assert not case.client.chat_completion.called


def test_lease_prevents_two_workers(case):
    with case.store.lease(case.job["id"]):
        with pytest.raises(CreativeError, match="BUSY"):
            run(case)
    assert case.store.get(case.job["id"])["state"] == "QUEUED"


def test_paid_execution_requires_explicit_worker_flag(case):
    with pytest.raises(CreativeError, match="PAID_EXECUTION_DISABLED"):
        run(case, paid=False)
    assert case.store.get(case.job["id"])["charged_cost_cny"] == "0"
    assert not case.client.chat_completion.called


def test_interrupted_worker_is_visible_without_reissuing_or_rewriting(case):
    with case.store.lease(case.job["id"]):
        saved = case.store.get(case.job["id"])
        saved["state"] = "GENERATING"
        case.store.save(saved)
        assert case.store.list()[0]["state"] == "GENERATING"
    listed = case.store.list()[0]
    assert listed["state"] == "NEEDS_RECONCILIATION" and listed["error_code"] == "CREATIVE_WORKER_INTERRUPTED"
    assert case.store.get(case.job["id"])["state"] == "GENERATING"
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        run(case)
    assert not case.client.chat_completion.called


def test_generated_sources_are_private_supplements_not_originals(case):
    request = freeze_draft(case.job, case.draft, case.sources)
    manifest = case.sources.manifest(request.bundle_hash)
    assert len(manifest.sources) == 6
    assert all(s.kind == "supplement" for s in manifest.sources)
    assert manifest.audience == "AUTHORING_ONLY"
    assert case.sources.verify(request.bundle_hash)["valid"]


def test_unknown_call_reservation_and_no_retry(case):
    case.client.chat_completion.side_effect = RuntimeError("PRIVATE_ERROR_SENTINEL")
    result = run(case)
    assert result["state"] == "NEEDS_RECONCILIATION"
    assert Decimal(result["charged_cost_cny"]) > 0
    assert "PRIVATE_ERROR_SENTINEL" not in str(result)
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        run(case)
    assert case.client.chat_completion.await_count == 1


def test_invalid_known_response_blocks_without_compiler(case):
    case.client.chat_completion.side_effect = [good_response({"arbitrary": "PRIVATE_MODEL_SENTINEL"})]
    result = run(case)
    assert result["state"] == "BLOCKED" and result["error_code"] == "CREATIVE_DRAFT_INVALID"
    assert result["receipt"]["usage_known"] and result["authoring_job_id"] is None
    assert "PRIVATE_MODEL_SENTINEL" not in str(result)


def test_wrong_role_count_is_refused(case):
    draft = case.draft.model_dump(); draft["characters"].append(draft["characters"][0])
    case.client.chat_completion.side_effect = [good_response(draft)]
    assert run(case)["error_code"] == "CREATIVE_CHARACTER_COUNT_INVALID"


def test_full_chain_requires_human_release_then_allows_selected_role(case):
    result = run(case)
    assert result["state"] == "COMPLETED", result
    assert case.client.chat_completion.await_count == 3
    assert validate_package(case.package)["valid"]
    assert Decimal(result["charged_cost_cny"]) <= Decimal(result["cost_ceiling_cny"])
    version = result["candidate_version_id"]
    with case.factory.begin() as db:
        pub = ScriptPublicationService(db, case.sources)
        gate = pub.gate_state(version, case.sources)
        assert not gate["can_approve"] and not gate["can_publish"] and gate["release"] is None
        assert PackageRuntimeService(db, pub).list_releases(2) == []
        report = fixture_report(case.package); report["findings"] = []; report["summary"] = "Explicit synthetic human review."
        body = SubmitAuditRequest(idempotency_key="human", expected_package_hash=gate["candidate"]["package_hash"], bundle_hash=gate["bundle_hash"], report=report)
        verified = case.sources.verify(gate["bundle_hash"], document=case.package)
        ScriptReviewService(db).save_audit(version, body, 1, verified, case.sources)
        gate = pub.gate_state(version, case.sources)
        decisions = [{"job_id": r["job_id"], "finding_id": f["id"], "status": "ACKNOWLEDGED", "note": "Synthetic reviewer explicitly checked provenance."}
            for r in gate["model_reports"] for f in r["report"]["findings"]]
        approval = pub.approve(version, ApprovePublicationRequest(idempotency_key="approve", expected_package_hash=gate["candidate"]["package_hash"], bundle_hash=gate["bundle_hash"], expected_basis_hash=gate["basis_hash"], model_dispositions=decisions, note="Synthetic reviewer authorizes publication."), 1, case.sources)
        assert pub.gate_state(version, case.sources)["release"] is None
        release = pub.publish(version, PublishPackageRequest(idempotency_key="publish", approval_id=approval["id"], expected_approval_hash=approval["approval_hash"], expected_basis_hash=gate["basis_hash"]), 1, case.sources)
        runtime = PackageRuntimeService(db, pub)
        catalog = runtime.list_releases(2)
        assert catalog[0]["title"] == case.request.title and len(catalog[0]["characters"]) == 3
        assert "FINAL-TRUTH" not in str(catalog) and "PRIVATE-" not in str(catalog)
        opening = runtime.create({"release_id": release["id"], "character_id": "c2", "idempotency_key": "opening"}, 2)
        service = PackagePlayService(db, pub)
        play = service.create({"opening_session_id": opening["session_id"], "idempotency_key": "play"}, 2)
        assert play["selected_character_id"] == "c2"
        assert "PRIVATE-2" in str(play) and "PRIVATE-0" not in str(play) and "FINAL-TRUTH" not in str(play)
        for index, action in enumerate(["ADVANCE_PHASE", "ADVANCE_PHASE", "SETTLE"]):
            play = service.act(play["play_id"], {"action": action, "expected_revision": play["revision"], "idempotency_key": f"step{index}"}, 2)
        assert play["settled"] and "FINAL-TRUTH" in str(play["settlement"])
        assert service.get(play["play_id"], 2)["settlement"] == play["settlement"]
    assert case.client.chat_completion.await_count == 3


def test_http_admin_queue_rejects_guests_and_never_dispatches(case):
    app = FastAPI()
    @app.middleware("http")
    async def identity(request: Request, call_next):
        value = request.headers.get("x-test-user")
        if value:
            request.state.current_user = SimpleNamespace(id=int(value), is_active=True, is_admin=value == "1")
        return await call_next(request)
    app.include_router(creative_routes.router)
    app.dependency_overrides[creative_routes.creative_store] = lambda: case.store
    client = TestClient(app)
    path = "/api/admin/fusion/creative-jobs"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"x-test-user": "2"}).status_code == 403
    assert client.post(path, headers={"x-test-user": "1"}, json=case.request.model_dump()).status_code == 202
    assert client.post(path, headers={"x-test-user": "1"}, json=case.request.model_dump() | {"publish": True}).status_code == 422
    assert client.get(path, headers={"x-test-user": "1"}).headers["cache-control"] == "no-store"
    assert not case.client.chat_completion.called
