"""Existing admin UI paths backed by CloudBase; no bootstrap or raw player files."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import re
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from src.cloudbase.admin import AdminError, CloudBaseAdmin, require_admin
from src.cloudbase.app import active_user, gateway_for, read_body
from src.cloudbase.storage import PrivateBlobStore, PrivateStorageError
from src.fusion.creative_generation import CreativeRequest
from src.fusion.package_validation import content_hash


router = APIRouter(prefix="/api/admin/fusion", tags=["CloudBase 剧本工作台"])


class ObjectRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")


async def active_admin(user: dict = Depends(active_user)) -> dict:
    require_admin(user)
    return user


def admin_for(request: Request) -> CloudBaseAdmin:
    # Tests/bootstrap may inject a backend adapter; HTTP input never selects an
    # arbitrary bucket, object URL, SQL table or account identity.
    adapter = getattr(request.app.state, "cloudbase_admin", None)
    if adapter is not None:
        return adapter
    gateway = gateway_for(request)
    bucket = os.environ.get("CLOUDBASE_PRIVATE_BUCKET", "")
    try:
        return CloudBaseAdmin(gateway, PrivateBlobStore(gateway, bucket) if bucket else None)
    except PrivateStorageError:
        raise AdminError("CLOUDBASE_SOURCE_STORAGE_NOT_CONFIGURED", 503) from None


def _id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 10 or not 0 < int(value) <= 2_147_483_647:
        raise HTTPException(422, "CLOUDBASE_ADMIN_IDENTIFIER_INVALID")
    return int(value)


def _digest(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise HTTPException(422, "CLOUDBASE_ADMIN_DIGEST_INVALID")
    return value


async def _body(request: Request, *, limit: int = 512 * 1024, allow_empty: bool = False) -> dict:
    return (await read_body(request, ObjectRequest, limit=limit, allow_empty=allow_empty)).model_dump(mode="json")


async def _run(request: Request, actor: dict, operation: str, body: dict | None = None) -> dict:
    return {"success": True, "data": await admin_for(request).run(actor, operation, body)}


def _money(value: Any) -> str:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1:
            raise ValueError
        return format(amount, "f")
    except (ValueError, InvalidOperation):
        raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502) from None


def _creative_view(detail: dict) -> dict:
    if (type(detail) is not dict or detail.get("kind") != "creative"
            or type(detail.get("actor_id")) is not int or detail["actor_id"] <= 0
            or type(detail.get("revision")) is not int or detail["revision"] < 0
            or detail.get("state") not in {"QUEUED", "RUNNING", "REVIEW_REQUIRED", "COMPLETED", "FAILED",
                                            "NEEDS_RECONCILIATION", "CANCELLED"}):
        raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
    try:
        if str(UUID(detail["id"])) != detail["id"]:
            raise ValueError
        creative_request = CreativeRequest.model_validate(detail.get("request_payload"))
    except (ValidationError, ValueError, KeyError, TypeError):
        raise AdminError("CLOUDBASE_CREATIVE_JOB_INCOMPATIBLE", 503) from None
    payload = cast(dict[str, Any], detail.get("payload"))
    if type(payload) is not dict:
        raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
    def reference(name: str):
        value = payload.get(name)
        if value is not None and (type(value) is not int or value <= 0):
            raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
        return value
    error = payload.get("error_code")
    if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error)):
        raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
    return {"id": detail["id"], "request": creative_request.model_dump(mode="json"),
            "state": detail["state"], "error_code": error,
            "authoring_job_id": reference("authoring_job_id"), "candidate_version_id": reference("candidate_version_id"),
            "charged_cost_cny": _money(detail.get("charged_cny")), "cost_ceiling_cny": _money(detail.get("budget_cny"))}


async def _jobs(request: Request, kind: str) -> list[dict]:
    gateway = gateway_for(request)
    result = await gateway.rpc("veil_archive_job_list", {
        "p_actor_id": None, "p_kind": kind, "p_limit": 100, "p_offset": 0})
    summaries = result.get("jobs")
    if type(summaries) is not list or len(summaries) > 100:
        raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
    jobs = []
    for summary in summaries:
        if (type(summary) is not dict or summary.get("kind") != kind
                or type(summary.get("id")) is not str or type(summary.get("actor_id")) is not int
                or summary["actor_id"] <= 0 or type(summary.get("revision")) is not int
                or summary["revision"] < 0 or summary.get("state") not in {
                    "QUEUED", "RUNNING", "REVIEW_REQUIRED", "COMPLETED", "FAILED", "NEEDS_RECONCILIATION", "CANCELLED"}):
            raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
        try:
            if str(UUID(summary["id"])) != summary["id"]:
                raise ValueError
        except ValueError:
            raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502) from None
        safe = {key: summary.get(key) for key in (
            "id", "actor_id", "kind", "state", "revision", "created_at", "updated_at")}
        safe.update(budget_cny=_money(summary.get("budget_cny")), charged_cny=_money(summary.get("charged_cny")))
        if kind != "creative":
            jobs.append(safe)
            continue
        detail = (await gateway.rpc("veil_archive_job_get", {"p_job_id": summary["id"]})).get("job")
        if (type(detail) is not dict or detail.get("id") != summary["id"] or detail.get("kind") != kind
                or detail.get("actor_id") != summary["actor_id"]):
            raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
        jobs.append(_creative_view(detail))
    return jobs


@router.get("/creative-jobs")
async def creative_jobs(request: Request, actor: dict = Depends(active_admin)):
    return {"success": True, "data": await _jobs(request, "creative")}


@router.get("/authoring-jobs")
async def authoring_jobs(request: Request, actor: dict = Depends(active_admin)):
    return {"success": True, "data": await _jobs(request, "authoring")}


@router.post("/creative-jobs")
@router.post("/authoring-jobs")
async def model_job_disabled(request: Request, actor: dict = Depends(active_admin)):
    await _body(request, limit=16384)
    raise AdminError("CLOUDBASE_MODEL_DISABLED", 503)


@router.post("/creative-jobs/{identifier}/cancel")
async def cancel_creative(identifier: str, request: Request, actor: dict = Depends(active_admin)):
    if await _body(request, limit=256, allow_empty=True):
        raise HTTPException(422, "CLOUDBASE_ADMIN_REQUEST_INVALID")
    try:
        if str(UUID(identifier)) != identifier:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "CLOUDBASE_ADMIN_IDENTIFIER_INVALID") from None
    gateway = gateway_for(request)
    result = await gateway.rpc("veil_archive_job_get", {"p_job_id": identifier})
    job = result.get("job")
    if type(job) is not dict or job.get("kind") != "creative":
        raise HTTPException(404, "CLOUDBASE_JOB_NOT_FOUND")
    _creative_view(job)
    keys = request.headers.getlist("idempotency-key")
    if len(keys) > 1:
        raise HTTPException(422, "CLOUDBASE_ADMIN_IDEMPOTENCY_REQUIRED")
    idem = keys[0] if keys else "cancel-" + identifier
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", idem):
        raise HTTPException(422, "CLOUDBASE_ADMIN_IDEMPOTENCY_REQUIRED")
    command = {"operation": "cancel-creative", "job_id": identifier,
               "admin_actor_id": actor["id"], "owner_actor_id": job["actor_id"], "kind": "creative"}
    cancelled = await gateway.rpc("veil_archive_job_cancel", {
        "p_job_id": identifier, "p_expected_revision": job["revision"], "p_admin_actor_id": actor["id"],
        "p_owner_actor_id": job["actor_id"], "p_kind": "creative", "p_idempotency_key": idem,
        "p_request_hash": content_hash(command)})
    snapshot = cancelled.get("job")
    if (type(snapshot) is not dict or snapshot.get("id") != identifier or snapshot.get("state") != "CANCELLED"
            or snapshot.get("actor_id") != job["actor_id"] or type(cancelled.get("replayed")) is not bool):
        raise AdminError("CLOUDBASE_JOB_RESPONSE_INVALID", 502)
    return {"success": True, "data": _creative_view(snapshot)}


@router.get("/review-candidates")
async def candidates(request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "candidate.list")


@router.post("/script-imports")
async def import_package(request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "candidate.import", await _body(request, limit=900_000))


@router.get("/script-imports/{job_id}")
async def import_job(job_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "import.get", {"job_id": _id(job_id)})


@router.get("/script-packages/{version_id}")
async def candidate(version_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "candidate.get", {"version_id": _id(version_id)})


@router.get("/script-packages/{version_id}/review")
async def review(version_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "review.get", {"version_id": _id(version_id)})


@router.get("/script-packages/{version_id}/publication")
async def publication(version_id: str, request: Request, bundle_hash: str | None = None,
                      actor: dict = Depends(active_admin)):
    body: dict = {"version_id": _id(version_id)}
    if bundle_hash is not None:
        body["bundle_hash"] = _digest(bundle_hash)
    return await _run(request, actor, "publication.get", body)


@router.post("/script-packages/{version_id}/audits")
async def manual_audit(version_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "manual_audit.create", {
        "version_id": _id(version_id), "request": await _body(request)})


@router.post("/script-audits/{audit_id}/dispositions")
async def dispose(audit_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "finding.dispose", {
        "audit_id": _id(audit_id), "request": await _body(request, limit=16384)})


@router.post("/script-packages/{version_id}/approvals")
async def approve(version_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "publication.approve", {
        "version_id": _id(version_id), "request": await _body(request, limit=512 * 1024)})


@router.post("/script-packages/{version_id}/publish")
async def publish(version_id: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "publication.publish", {
        "version_id": _id(version_id), "request": await _body(request, limit=16384)})


@router.get("/source-bundles")
async def sources(request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "source.list")


@router.get("/source-bundles/{bundle_hash}")
async def source_bundle(bundle_hash: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "source.get", {"bundle_hash": _digest(bundle_hash)})


@router.get("/source-bundles/{bundle_hash}/sources/{source_id}")
async def source_bytes(bundle_hash: str, source_id: str, request: Request, actor: dict = Depends(active_admin)):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", source_id):
        raise HTTPException(422, "CLOUDBASE_ADMIN_IDENTIFIER_INVALID")
    data, media = await admin_for(request).run(actor, "source.read", {
        "bundle_hash": _digest(bundle_hash), "source_id": source_id})
    return Response(content=data, media_type=media if media.startswith("image/") else "text/plain",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; sandbox", "X-Frame-Options": "DENY"})


@router.post("/source-bundles/{bundle_hash}/verify")
async def verify_source(bundle_hash: str, request: Request, actor: dict = Depends(active_admin)):
    await _body(request, limit=256, allow_empty=True)
    return await _run(request, actor, "source.verify", {"bundle_hash": _digest(bundle_hash)})


@router.get("/source-verifications/{report_hash}")
async def source_report(report_hash: str, request: Request, actor: dict = Depends(active_admin)):
    return await _run(request, actor, "source.report", {"report_hash": _digest(report_hash)})


@router.post("/script-packages/{version_id}/verify-sources")
async def verify_candidate(version_id: str, request: Request, actor: dict = Depends(active_admin)):
    body = await _body(request, limit=4096)
    if set(body) != {"bundle_hash"} or not isinstance(body.get("bundle_hash"), str):
        raise HTTPException(422, "CLOUDBASE_ADMIN_REQUEST_INVALID")
    return await _run(request, actor, "candidate.verify", {
        "version_id": _id(version_id), "bundle_hash": _digest(body["bundle_hash"])})


@router.get("/script-packages/{version_id}/rule-review")
async def rule_review(version_id: str, request: Request, bundle_hash: str, expected_package_hash: str,
                      offset: str = "0", limit: str = "20", actor: dict = Depends(active_admin)):
    if (not offset.isascii() or not offset.isdecimal() or len(offset) > 5
            or not limit.isascii() or not limit.isdecimal() or len(limit) > 2
            or not 0 <= int(offset) <= 15100 or not 1 <= int(limit) <= 50):
        raise HTTPException(422, "CLOUDBASE_ADMIN_PAGE_INVALID")
    return await _run(request, actor, "rule_review.get", {"version_id": _id(version_id),
        "bundle_hash": _digest(bundle_hash), "expected_package_hash": _digest(expected_package_hash),
        "offset": int(offset), "limit": int(limit)})
