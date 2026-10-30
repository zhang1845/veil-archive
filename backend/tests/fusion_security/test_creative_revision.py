"""Offline regression for bounded creative revision; synthetic content and SDK only."""
import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.base import SQLAlchemyBase
from src.db.models import User
from src.fusion import authoring_model, creative_generation
from src.fusion.authoring_jobs import AuthoringJobError, AuthoringJobStore
from src.fusion.budget import BudgetPolicy
from src.fusion.creative_generation import CreativeError, CreativeRequest, CreativeStore, run_creative_job
from src.fusion.package_import import PackageImportService
from src.fusion.package_play import PackagePlayService
from src.fusion.package_runtime import PackageRuntimeService
from src.fusion.package_validation import content_hash
from src.fusion.script_publication import PublicationConflict, ScriptPublicationService
from src.fusion.script_review import ScriptReviewService
from src.fusion.source_bundles import SourceBundleStore
from src.schemas.script_publication import ApprovePublicationRequest, PublishPackageRequest
from src.schemas.script_review import SubmitAuditRequest
from src.services.llm_service import ToolCall
from tests.fusion_security.creative_fixture import compiled_package, story
from tests.fusion_security.test_authoring_model import draft_for, fixture_config, fixture_report, good_response


def changed_story(index):
    draft = story().model_dump()
    if index:
        draft["evidence"][0]["text"] += f"复核记录第{index}版补充了停钟发现时刻，便于和交接单核对。"
    return creative_generation.CreativeDraft.model_validate(draft)


@pytest.fixture
def make_revision_case(tmp_path, monkeypatch):
    """All files, identities, packages, ledgers and source bodies are disposable."""
    engine = create_engine(f"sqlite:///{tmp_path / 'revision.sqlite'}", connect_args={"check_same_thread": False, "autocommit": False})
    SQLAlchemyBase.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False)
    with factory.begin() as db:
        db.add(User(id=1, username="revision-admin", email="admin@example.invalid", hashed_password="NO_LOGIN", is_admin=True, is_active=True))
    counter = 0

    def make(*, findings=("WARNING", None), max_rounds=2, ceiling="0.50", unchanged=False,
             fail_at=None, invalid_revision=False, full_usage=False, revision_mutation=None,
             revision_response_patch=None, report_transform=None):
        nonlocal counter
        counter += 1
        sources = SourceBundleStore(tmp_path / f"sources-{counter}")
        store = CreativeStore(tmp_path / f"creative-{counter}")
        jobs = AuthoringJobStore(factory)
        request = CreativeRequest(idempotency_key=f"revision-{counter}", title="虚构小站修订测试", theme="完全虚构的小站登记簿推理案件，用于离线修稿验证。", player_count=3,
                                  max_revision_rounds=max_rounds, cost_ceiling_cny=ceiling)
        job = store.create(request, 1)
        config = fixture_config()
        if full_usage:
            config = replace(config, pricing=BudgetPolicy(100000, Decimal("0.1"), Decimal("1"), Decimal("0.2"), Decimal("2"), True, "revision-fixture-rates-v1"))
        state = SimpleNamespace(store=store, sources=sources, jobs=jobs, factory=factory, request=request, job=job,
                                config=config, script_calls=0, audit_calls=0, compile_calls=0,
                                drafts=[], packages=[], reports=[], phase_calls=[], revision_inputs=[])

        async def respond(*args, **kwargs):
            messages = args[0]
            system = messages[0].content
            if "原创推理剧本作者" in system or "原创推理剧本修订编辑" in system:
                index = state.script_calls
                phase = "DRAFT" if index == 0 else "REVISE"
                state.phase_calls.append((phase, index))
                state.script_calls += 1
                if fail_at == (phase, index):
                    raise RuntimeError("PRIVATE_REVISION_PROVIDER_SENTINEL")
                if invalid_revision and index:
                    return good_response({"invalid": "PRIVATE_INVALID_REVISION_SENTINEL"})
                draft = changed_story(0 if unchanged else index)
                state.drafts.append(draft)
                payload = draft.model_dump()
                if index:
                    state.revision_inputs.append(json.loads(messages[1].content))
                    if revision_mutation:
                        revision_mutation(payload)
                response = good_response(payload)
                if index and revision_response_patch:
                    for key, value in revision_response_patch.items():
                        setattr(response, key, value)
                saved = store.get(job["id"])
                assert Decimal(saved["charged_cost_cny"]) > 0
                if full_usage:
                    rounds = saved.get("rounds", [])
                    receipt = rounds[-1].get("receipt") if rounds else saved.get("receipt")
                    reservation = receipt["reservation"]
                    response.usage = {"prompt_tokens": reservation["prompt_tokens"], "completion_tokens": reservation["completion_tokens"] - 16,
                                      "total_tokens": reservation["prompt_tokens"] + reservation["completion_tokens"] - 16}
                return response
            current = store.get(job["id"])
            context = jobs.inputs(current["authoring_job_id"])["context"]
            index = state.script_calls - 1
            package = compiled_package(context, state.drafts[-1])
            phase = "COMPILE" if kwargs.get("max_completion_tokens") == 8192 else "AUDIT"
            state.phase_calls.append((phase, index))
            if fail_at == (phase, index):
                raise RuntimeError("PRIVATE_REVISION_PROVIDER_SENTINEL")
            if phase == "COMPILE":
                state.compile_calls += 1
                state.packages.append(package)
                response = good_response(draft_for(package))
            else:
                state.audit_calls += 1
                report = fixture_report(package)
                severity = findings[min(index, len(findings) - 1)]
                if severity is None:
                    report["findings"] = []
                else:
                    report["findings"][0].update(category="TIMELINE", severity=severity, message=f"Synthetic timeline finding in revision {index}.")
                if report_transform:
                    report_transform(report)
                state.reports.append(deepcopy(report))
                response = good_response(report)
            if full_usage:
                attempt = next(item for item in jobs.get(current["authoring_job_id"])["attempts"] if item["step"] == phase)
                reservation = attempt["prepared"]["reservation"]
                response.usage = {"prompt_tokens": reservation["prompt_tokens"], "completion_tokens": reservation["completion_tokens"] - 16,
                                  "total_tokens": reservation["prompt_tokens"] + reservation["completion_tokens"] - 16}
            return response

        client = SimpleNamespace(chat_completion=AsyncMock(side_effect=respond))
        monkeypatch.setattr(creative_generation, "OpenAILLMService", lambda **kwargs: client)
        monkeypatch.setattr(authoring_model, "OpenAILLMService", lambda **kwargs: client)
        state.client = client
        return state

    yield make
    engine.dispose()


def execute(case):
    return asyncio.run(run_creative_job(case.job["id"], case.store, case.sources, case.jobs, case.config, allow_paid=True))


@pytest.mark.parametrize("patch", [{"max_revision_rounds": -1}, {"max_revision_rounds": 4}, {"max_revision_rounds": True},
                                   {"max_revision_rounds": "2"}, {"cost_ceiling_cny": "0.21"}, {"cost_ceiling_cny": 0.5},
                                   {"publish": True}, {"workflow_version": "untrusted"}])
def test_revision_request_rejects_invalid_limits_or_privileged_fields(make_revision_case, patch):
    case = make_revision_case()
    with pytest.raises(ValidationError):
        CreativeRequest.model_validate(case.request.model_dump() | patch)


def test_revision_clean_first_audit_stops_without_editing_or_publishing(make_revision_case):
    case = make_revision_case(findings=(None,))
    result = execute(case)
    assert result["state"] == "COMPLETED" and result["stop_reason"] == "CLEAN_AUDIT", result
    assert len(result["rounds"]) == 1 and result["current_round"] == 0
    assert case.client.chat_completion.await_count == 3
    assert result["rounds"][0]["findings"] == []
    with case.factory.begin() as db:
        gate = ScriptPublicationService(db, case.sources).gate_state(result["candidate_version_id"], case.sources)
        assert not gate["can_approve"] and not gate["can_publish"] and gate["release"] is None


def test_revision_information_only_is_preserved_without_triggering_rewrite(make_revision_case):
    case = make_revision_case(findings=("INFO",))
    result = execute(case)
    assert result["state"] == "COMPLETED" and result["stop_reason"] == "CLEAN_AUDIT", result
    assert result["rounds"][0]["findings"][0]["severity"] == "INFO"
    assert case.client.chat_completion.await_count == 3


@pytest.mark.parametrize("severity", ["WARNING", "BLOCKER"])
def test_revision_finding_creates_new_frozen_source_candidate_and_audit(make_revision_case, severity):
    case = make_revision_case(findings=(severity, None))
    result = execute(case)
    assert result["state"] == "COMPLETED" and result["stop_reason"] == "CLEAN_AUDIT", result
    assert case.client.chat_completion.await_count == 6
    assert [r["round_index"] for r in result["rounds"]] == [0, 1]
    assert [r["content_version"] for r in result["rounds"]] == ["ai-v1", "ai-v2"]
    first, final = result["rounds"]
    for field in ("authoring_job_id", "candidate_version_id", "bundle_hash", "draft_hash", "report_hash"):
        assert first[field] and final[field] and first[field] != final[field], field
    assert first["findings"][0]["severity"] == severity and final["findings"] == []
    assert "evidence" in final["changed_fields"]
    assert result["candidate_version_id"] == final["candidate_version_id"]
    with case.factory.begin() as db:
        versions = [PackageImportService(db).get_version(r["candidate_version_id"]) for r in (first, final)]
        assert versions[0]["package"]["script_key"] == versions[1]["package"]["script_key"]
        assert versions[0]["package_hash"] != versions[1]["package_hash"]
        for r, version in zip((first, final), versions):
            assert case.sources.verify(r["bundle_hash"], document=version["package"])["valid"]
            gate = ScriptPublicationService(db, case.sources).gate_state(r["candidate_version_id"], case.sources)
            assert not gate["can_approve"] and not gate["can_publish"] and gate["release"] is None
    old_audit = next(a["output"] for a in case.jobs.get(first["authoring_job_id"])["attempts"] if a["step"] == "AUDIT")
    assert old_audit["findings"] == first["findings"]
    assert Decimal(result["charged_cost_cny"]) == sum((Decimal(r["charged_cost_cny"]) for r in result["rounds"]), Decimal(0))
    assert Decimal(result["charged_cost_cny"]) <= Decimal(result["cost_ceiling_cny"])


def test_revision_unresolved_findings_stop_at_configured_round_limit(make_revision_case):
    case = make_revision_case(findings=("WARNING",), max_rounds=1)
    result = execute(case)
    assert result["state"] == "REVIEW_REQUIRED" and result["stop_reason"] == "ROUND_LIMIT", result
    assert len(result["rounds"]) == 2 and case.client.chat_completion.await_count == 6
    assert all(r["findings"] for r in result["rounds"])


def test_revision_zero_edit_limit_preserves_single_pass_behavior(make_revision_case):
    case = make_revision_case(findings=("WARNING",), max_rounds=0, ceiling="0.20")
    result = execute(case)
    assert result["state"] == "COMPLETED" and result["stop_reason"] == "ROUND_LIMIT", result
    assert len(result["rounds"]) == 1 and case.client.chat_completion.await_count == 3


def test_revision_identical_rewrite_stops_without_duplicate_compile(make_revision_case):
    case = make_revision_case(findings=("WARNING",), unchanged=True)
    result = execute(case)
    assert result["state"] == "REVIEW_REQUIRED" and result["stop_reason"] == "NO_CHANGE", result
    assert case.script_calls == 2 and case.compile_calls == 1 and case.audit_calls == 1
    assert case.client.chat_completion.await_count == 4
    assert result["candidate_version_id"] == result["rounds"][0]["candidate_version_id"]


def test_revision_total_budget_stops_before_starting_an_unaffordable_round(make_revision_case):
    case = make_revision_case(findings=("WARNING",), max_rounds=3, ceiling="0.20", full_usage=True)
    result = execute(case)
    assert result["state"] == "REVIEW_REQUIRED" and result["stop_reason"] == "BUDGET_LIMIT", result
    assert case.script_calls < 4 and case.audit_calls >= 1
    assert Decimal(result["charged_cost_cny"]) <= Decimal("0.20")
    assert all(r["findings"] for r in result["rounds"] if r["candidate_version_id"] is not None)


@pytest.mark.parametrize("phase,index", [("REVISE", 1), ("COMPILE", 1), ("AUDIT", 1)])
def test_revision_unknown_call_is_not_reissued_and_keeps_all_reservations(make_revision_case, phase, index):
    case = make_revision_case(findings=("WARNING",), fail_at=(phase, index))
    result = execute(case)
    assert result["state"] == "NEEDS_RECONCILIATION", result
    assert Decimal(result["charged_cost_cny"]) > 0
    assert result["rounds"][0]["findings"]
    assert result["rounds"][0]["candidate_version_id"] is not None
    assert "PRIVATE_REVISION_PROVIDER_SENTINEL" not in str(result)
    count = case.client.chat_completion.await_count
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        execute(case)
    assert case.client.chat_completion.await_count == count


def test_revision_invalid_rewrite_preserves_previous_candidate_and_stops(make_revision_case):
    case = make_revision_case(findings=("WARNING",), invalid_revision=True)
    result = execute(case)
    assert result["state"] == "BLOCKED", result
    assert case.client.chat_completion.await_count == 4
    assert result["rounds"][0]["candidate_version_id"] is not None
    assert "PRIVATE_INVALID_REVISION_SENTINEL" not in str(result)


def test_revision_interrupted_editor_is_visible_without_replaying(make_revision_case):
    case = make_revision_case()
    job = case.store.get(case.job["id"])
    job["state"] = "REVISING"
    case.store.save(job)
    assert case.store.list()[0]["state"] == "NEEDS_RECONCILIATION"
    assert case.store.get(job["id"])["state"] == "REVISING"
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        execute(case)
    assert not case.client.chat_completion.called


def test_revision_limits_are_bound_to_original_idempotency_key(make_revision_case):
    case = make_revision_case()
    assert case.store.create(case.request, 1) == case.job
    for patch in ({"max_revision_rounds": 3}, {"cost_ceiling_cny": "1.00"}):
        with pytest.raises(CreativeError, match="CONFLICT"):
            case.store.create(case.request.model_copy(update=patch), 1)
    assert not case.client.chat_completion.called


def test_revision_legacy_job_without_workflow_marker_is_still_single_pass(make_revision_case):
    case = make_revision_case(findings=("WARNING",), max_rounds=0, ceiling="0.20")
    legacy = case.store.get(case.job["id"])
    for field in ("workflow_version", "rounds", "current_round", "stop_reason"):
        legacy.pop(field, None)
    legacy["request"].pop("max_revision_rounds", None)
    legacy["request"].pop("cost_ceiling_cny", None)
    case.store.save(legacy)
    result = execute(case)
    assert result["state"] == "COMPLETED", result
    assert case.client.chat_completion.await_count == 3
    assert "workflow_version" not in result


def test_revision_editor_receives_entire_audit_and_bound_target_content(make_revision_case):
    case = make_revision_case(findings=("WARNING", None))
    result = execute(case)
    assert result["state"] == "COMPLETED", result
    payload = case.revision_inputs[0]
    with case.factory.begin() as db:
        package = PackageImportService(db).get_version(result["rounds"][0]["candidate_version_id"])["package"]
    assert payload["previous_draft"] == case.drafts[0].model_dump()
    assert payload["audit_report"] == authoring_model.validate_model_audit(case.reports[0], package)
    assert payload["candidate_hash"] == content_hash(package)
    assert payload["candidate_targets"] == [{"target": {"collection": "introduction", "id": None}, "content": package["introduction"]}]


@pytest.mark.parametrize("mutation,error", [
    (lambda p: p["characters"].append(deepcopy(p["characters"][0])), "CREATIVE_CHARACTER_COUNT_INVALID"),
    (lambda p: p["characters"][1].update(name=p["characters"][0]["name"]), "CREATIVE_CHARACTER_COUNT_INVALID"),
    (lambda p: p["characters"][0].update(name="未经允许的新角色"), "CREATIVE_REVISION_CHARACTER_CHANGED"),
    (lambda p: p.update(approve=True), "CREATIVE_DRAFT_INVALID"),
])
def test_revision_editor_cannot_change_roster_or_grant_approval(make_revision_case, mutation, error):
    case = make_revision_case(revision_mutation=mutation)
    result = execute(case)
    assert result["state"] == "BLOCKED" and result["error_code"] == error, result
    assert case.client.chat_completion.await_count == 4
    assert case.compile_calls == 1 and case.audit_calls == 1
    assert result["candidate_version_id"] == result["rounds"][0]["candidate_version_id"]


@pytest.mark.parametrize("patch", [
    {"model": "unauthorized-model"}, {"finish_reason": "length"},
    {"tool_calls": [ToolCall(name="publish", arguments={})]},
    {"reasoning_content": "unrequested internal text"},
])
def test_revision_editor_response_contract_refuses_unexpected_completion(make_revision_case, patch):
    case = make_revision_case(revision_response_patch=patch)
    result = execute(case)
    assert result["state"] == "BLOCKED" and result["error_code"] == "CREATIVE_RESPONSE_INVALID", result
    assert case.client.chat_completion.await_count == 4
    assert case.compile_calls == 1 and case.audit_calls == 1


def test_revision_missing_usage_preserves_reservation_and_needs_reconciliation(make_revision_case):
    case = make_revision_case(revision_response_patch={"usage": None})
    result = execute(case)
    assert result["state"] == "NEEDS_RECONCILIATION" and result["error_code"] == "CREATIVE_USAGE_UNKNOWN", result
    receipt = result["rounds"][-1]["receipt"]
    assert receipt["usage_known"] is False
    assert receipt["charged_cost_cny"] == receipt["reservation"]["cost_cny"]
    assert case.client.chat_completion.await_count == 4


def test_revision_oversize_audit_stops_without_truncating_or_dropping_findings(make_revision_case):
    def enlarge(report):
        template = report["findings"][0]
        report["findings"] = [deepcopy(template) | {"id": f"oversize-{i}", "message": "完整保留的虚构审核说明。" * 200} for i in range(12)]
    case = make_revision_case(findings=("WARNING",), report_transform=enlarge)
    result = execute(case)
    assert result["state"] == "BLOCKED" and result["error_code"] == "CREATIVE_REVISION_INPUT_TOO_LARGE", result
    assert case.client.chat_completion.await_count == 3 and case.script_calls == 1
    expected = authoring_model.validate_model_audit(case.reports[0], case.packages[0])
    assert result["rounds"][0]["findings"] == expected["findings"]
    assert len(result["rounds"][0]["findings"]) == 12


def test_revision_private_draft_archives_do_not_break_list_or_replace_original(make_revision_case):
    case = make_revision_case(findings=("WARNING", None))
    result = execute(case)
    assert result["state"] == "COMPLETED", result
    files = sorted((case.store.root / "drafts").glob("*.json"))
    assert len(files) == 2
    original_bytes = [p.read_bytes() for p in files]
    artifacts = [json.loads(body) for body in original_bytes]
    for index, artifact in enumerate(artifacts):
        assert artifact["draft"] == case.drafts[index].model_dump()
        assert artifact["draft_hash"] == content_hash(artifact["draft"])
        assert artifact["draft_hash"] == result["rounds"][index]["draft_hash"]
        assert files[index].stat().st_mode & 0o777 == 0o600
    assert artifacts[0]["previous_draft_hash"] is None
    assert artifacts[1]["previous_draft_hash"] == artifacts[0]["draft_hash"]
    assert artifacts[1]["review_basis_hash"] == result["rounds"][0]["report_hash"]
    # Historical archive names in the root and files in the private subdirectory
    # must not be interpreted as queue jobs by the list API.
    (case.store.root / f"{case.job['id']}.round-9.json").write_text("PRIVATE_UNLISTED_ARCHIVE")
    listed = case.store.list()
    assert len(listed) == 1 and listed[0]["id"] == case.job["id"]
    assert "FINAL-TRUTH" not in str(listed) and "PRIVATE_UNLISTED_ARCHIVE" not in str(listed)
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        execute(case)
    assert [p.read_bytes() for p in files] == original_bytes


@pytest.mark.parametrize("child_state", ["QUEUED", "RUNNING", "RESERVED"])
def test_revision_child_worker_contention_requires_reconciliation_without_replay(make_revision_case, monkeypatch, child_state):
    case = make_revision_case(findings=(None,))
    attempts = []

    async def claimed_elsewhere(runner, identifier, *, allow_paid=False):
        assert allow_paid is True
        attempts.append(identifier)
        if child_state in {"RUNNING", "RESERVED"}:
            token = case.jobs.claim(identifier)
            if child_state == "RESERVED":
                context = case.jobs.inputs(identifier)["context"]
                prepared = runner.model.prepare("COMPILE", context)
                metadata = {"step": "COMPILE", "prompt_hash": prepared.prompt_hash,
                            "contract_hash": prepared.contract_hash, "input_tokens": prepared.input_tokens,
                            "max_completion_tokens": prepared.max_completion_tokens,
                            "reservation": prepared.reservation.to_metadata(), "request_contract": prepared.request_contract}
                case.jobs.reserve(identifier, token, "COMPILE", metadata)
        raise AuthoringJobError("AUTHORING_LEASE_BUSY")

    monkeypatch.setattr(creative_generation.AuthoringRunner, "run", claimed_elsewhere)
    result = execute(case)
    assert result["state"] == "NEEDS_RECONCILIATION" and result["stop_reason"] == "RESULT_UNKNOWN", result
    assert len(attempts) == 1 and case.client.chat_completion.await_count == 1
    child = case.jobs.get(result["authoring_job_id"])
    assert child["state"] == ("QUEUED" if child_state == "QUEUED" else "RUNNING")
    if child_state == "RESERVED":
        assert child["attempts"][0]["status"] == "RESERVED"
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        execute(case)
    assert len(attempts) == 1 and case.client.chat_completion.await_count == 1


class SyntheticWorkerShutdown(BaseException):
    """Simulates process termination without invoking real signals or processes."""


@pytest.mark.parametrize("shutdown", [SyntheticWorkerShutdown, asyncio.CancelledError])
def test_revision_child_dispatch_persists_full_parent_allowance_before_interruption(make_revision_case, shutdown):
    case = make_revision_case(findings=(None,))
    respond = case.client.chat_completion.side_effect
    observed = []

    async def interrupt_child(*args, **kwargs):
        messages = args[0]
        if "原创推理剧本作者" not in messages[0].content and kwargs.get("max_completion_tokens") == 8192:
            saved = case.store.get(case.job["id"])
            item = saved["rounds"][-1]
            assert Decimal(item["authoring_cost_cny"]) == Decimal("0.10")
            expected = Decimal(item["receipt"]["charged_cost_cny"]) + Decimal("0.10")
            assert Decimal(saved["charged_cost_cny"]) == expected
            child = case.jobs.get(item["authoring_job_id"])
            assert child["state"] == "RUNNING" and child["attempts"][0]["status"] == "IN_FLIGHT"
            observed.append(expected)
            raise shutdown()
        return await respond(*args, **kwargs)

    case.client.chat_completion.side_effect = interrupt_child
    with pytest.raises(shutdown):
        execute(case)
    assert len(observed) == 1 and case.client.chat_completion.await_count == 2
    saved = case.store.get(case.job["id"])
    assert Decimal(saved["charged_cost_cny"]) == observed[0]
    assert Decimal(saved["rounds"][-1]["authoring_cost_cny"]) == Decimal("0.10")
    listed = case.store.list()[0]
    assert listed["state"] == "NEEDS_RECONCILIATION" and listed["stop_reason"] == "RESULT_UNKNOWN"
    assert Decimal(listed["charged_cost_cny"]) == observed[0]
    with pytest.raises(CreativeError, match="NOT_RETRYABLE"):
        execute(case)
    assert case.client.chat_completion.await_count == 2


def test_revision_final_candidate_requires_manual_confirmation_before_release(make_revision_case):
    case = make_revision_case(findings=("WARNING", None))
    result = execute(case)
    assert result["state"] == "COMPLETED" and result["stop_reason"] == "CLEAN_AUDIT", result
    first, final = result["rounds"]
    assert final["content_version"] == "ai-v2" and case.client.chat_completion.await_count == 6
    with case.factory.begin() as db:
        db.add(User(id=2, username="revision-player", email="player@example.invalid", hashed_password="NO_LOGIN", is_admin=False, is_active=True))
        db.flush()
        pub = ScriptPublicationService(db, case.sources)
        version_id = final["candidate_version_id"]
        version = PackageImportService(db).get_version(version_id)
        gate = pub.gate_state(version_id, case.sources)
        assert gate["bundle_hash"] == final["bundle_hash"] != first["bundle_hash"]
        assert not gate["can_approve"] and not gate["can_publish"] and gate["release"] is None
        assert PackageRuntimeService(db, pub).list_releases(2) == []
        # A successful automatic loop cannot stand in for the administrator's
        # separate manual report, confirmation and publication decisions.
        with pytest.raises(PublicationConflict, match="PUBLICATION_APPROVAL_EXPIRED"):
            pub.publish(version_id, PublishPackageRequest(idempotency_key="before-confirmation", approval_id=1,
                expected_approval_hash="1" * 64, expected_basis_hash=gate["basis_hash"]), 1, case.sources)
        report = fixture_report(version["package"])
        report.update(summary="Explicit synthetic human review of the final revised manuscript in all five dimensions.", findings=[])
        assert set(report["coverage"]) == {"PROVENANCE", "TIMELINE", "EVIDENCE", "KNOWLEDGE_BOUNDARY", "PLAYABILITY"}
        verified = case.sources.verify(final["bundle_hash"], document=version["package"])
        ScriptReviewService(db).save_audit(version_id, SubmitAuditRequest(idempotency_key="final-human-audit",
            expected_package_hash=version["package_hash"], bundle_hash=final["bundle_hash"], report=report), 1, verified, case.sources)
        gate = pub.gate_state(version_id, case.sources)
        assert gate["can_approve"] and not gate["can_publish"] and gate["release"] is None
        assert [r["job_id"] for r in gate["model_reports"]] == [final["authoring_job_id"]]
        approval = pub.approve(version_id, ApprovePublicationRequest(idempotency_key="final-human-confirmation",
            expected_package_hash=version["package_hash"], bundle_hash=final["bundle_hash"],
            expected_basis_hash=gate["basis_hash"], model_dispositions=[],
            note="The synthetic administrator confirms only the final revised candidate."), 1, case.sources)
        assert pub.gate_state(version_id, case.sources)["release"] is None
        release = pub.publish(version_id, PublishPackageRequest(idempotency_key="final-explicit-publication",
            approval_id=approval["id"], expected_approval_hash=approval["approval_hash"], expected_basis_hash=gate["basis_hash"]), 1, case.sources)
        assert release["version_id"] == version_id
        assert release["package_hash"] == version["package_hash"]
        assert release["bundle_hash"] == final["bundle_hash"]
        assert release["basis_hash"] == gate["basis_hash"]
        assert pub.get_release(release["id"])["content_version"] == "ai-v2"
        assert pub.gate_state(first["candidate_version_id"], case.sources)["release"] is None
        runtime = PackageRuntimeService(db, pub)
        catalog = runtime.list_releases(2)
        assert len(catalog) == 1 and catalog[0]["id"] == release["id"]
        assert "FINAL-TRUTH" not in str(catalog) and "PRIVATE-" not in str(catalog)
        opening = runtime.create({"release_id": release["id"], "character_id": "c1", "idempotency_key": "final-opening"}, 2)
        play = PackagePlayService(db, pub).create({"opening_session_id": opening["session_id"], "idempotency_key": "final-play"}, 2)
        assert play["selected_character_id"] == "c1"
        assert "PRIVATE-1" in str(play) and "PRIVATE-0" not in str(play) and "PRIVATE-2" not in str(play)
        assert "FINAL-TRUTH" not in str(play) and play["settled"] is False
    assert case.client.chat_completion.await_count == 6
