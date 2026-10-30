"""CloudBase persistence for existing deterministic administrator services.

``actor`` MUST be the server's freshly authenticated auth.me DTO, never an HTTP
body. One private global aggregate owns only this application's candidate,
review and formal-publication rows. No production users/passwords or player
workspaces are copied. SQLite is an ephemeral calculation UoW; PostgreSQL CAS
is the durable transaction. No model/provider or background worker runs here.

CloudBaseAdmin.run supports route-oriented operations (request bodies nested
under ``request`` for routes with a version/audit path parameter). Results are
the existing service DTO, without an HTTP envelope. Unsupported operations fail
explicitly. Source registration is a separate server/bootstrap method and must
not be mounted as a public HTTP endpoint.
"""
from __future__ import annotations

import copy
from datetime import datetime
import re
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import DateTime, MetaData, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.cloudbase.gateway import CloudBaseGateway, GatewayConflict, GatewayError, canonical_json
from src.cloudbase.storage import PrivateBlobStore, PrivateStorageError
from src.db.models.authoring_job import AuthoringAttempt, AuthoringJob
from src.db.models.script_package import ScriptImportJob, ScriptPackageVersion
from src.db.models.script_publication import ScriptPackageRelease, ScriptPublicationApproval
from src.db.models.script_review import ScriptAuditRecord, ScriptFindingDisposition
from src.db.models.user import User
from src.fusion.package_import import PackageConflict, PackageImportService, PackageNotFound
from src.fusion.package_validation import PackageInputError, content_hash, validate_package
from src.fusion.rule_review import RuleReviewError, build_rule_review
from src.fusion.script_publication import PublicationConflict, PublicationError, ScriptPublicationService
from src.fusion.script_review import ReviewInputError, ScriptReviewService
from src.fusion.source_bundles import SourceBundleError, SourceBundleNotFound
from src.schemas.script_package import ImportPackageRequest
from src.schemas.script_publication import ApprovePublicationRequest, PublishPackageRequest
from src.schemas.script_review import FindingDispositionRequest, parse_submit_audit_request


WORKSPACE_SCHEMA = "cloudbase-admin-workspace/1.0"
MAX_WORKSPACE_BYTES = 900_000
MODELS = {
    "versions": ScriptPackageVersion, "imports": ScriptImportJob,
    "audits": ScriptAuditRecord, "dispositions": ScriptFindingDisposition,
    "approvals": ScriptPublicationApproval, "releases": ScriptPackageRelease,
    "authoring_jobs": AuthoringJob, "authoring_attempts": AuthoringAttempt,
}
_READS = frozenset({"candidate.list", "candidate.get", "candidate.check", "import.get",
                    "review.get", "publication.get", "rule_review.get", "source.list",
                    "source.get", "source.read", "source.report"})
_WRITES = frozenset({"candidate.import", "candidate.verify", "manual_audit.create",
                     "finding.dispose", "publication.approve", "publication.publish", "source.verify"})
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


class AdminError(GatewayError):
    pass


def require_admin(actor: Any) -> int:
    if (type(actor) is not dict or type(actor.get("id")) is not int or actor["id"] <= 0
            or actor.get("is_admin") is not True or actor.get("is_active") is not True
            or actor.get("is_guest") is True):
        raise AdminError("CLOUDBASE_ADMIN_REQUIRED", 403)
    return actor["id"]


def _identifier(body: dict, field: str) -> int:
    value = body.get(field)
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise AdminError("CLOUDBASE_ADMIN_IDENTIFIER_INVALID", 422)
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise AdminError("CLOUDBASE_ADMIN_DIGEST_INVALID", 422)
    return value


class _UnavailableSourceStore:
    """A closed publication gate when no source scope has been registered."""
    def verify(self, *args, **kwargs):
        raise SourceBundleNotFound("来源尚未配置")

    def get_report(self, *args, **kwargs):
        raise SourceBundleNotFound("来源尚未配置")


class AdminWorkspace:
    def __init__(self, actor: dict | None, payload: dict | None, *, _runtime_readonly: bool = False):
        # Internal runtime projection has no actor and cannot grant an admin
        # identity; only CloudBaseAdmin.read_runtime_manifests uses this mode.
        self.actor_id = 0 if _runtime_readonly and actor is None else require_admin(actor)
        source = copy.deepcopy(payload if payload is not None else self.empty())
        if (type(source) is not dict or set(source) != {"schema_version", "actors", "rows", "source_archives"}
                or source.get("schema_version") != WORKSPACE_SCHEMA or type(source.get("rows")) is not dict
                or set(source["rows"]) != set(MODELS) or type(source.get("actors")) is not list
                or len(source["actors"]) > 100
                or any(type(item) is not int or item <= 0 for item in source["actors"])
                or len(set(source["actors"])) != len(source["actors"])
                or type(source.get("source_archives")) is not dict or len(source["source_archives"]) > 30
                or any(not isinstance(key, str) or not _DIGEST.fullmatch(key) or type(value) is not dict
                       for key, value in source["source_archives"].items())
                or any(type(rows) is not list or len(rows) > 1000 for rows in source["rows"].values())):
            raise AdminError("CLOUDBASE_ADMIN_WORKSPACE_INVALID", 409)
        self._bounded(source)
        self.actors = set(source["actors"]) | ({self.actor_id} if self.actor_id else set())
        if len(self.actors) > 100:
            raise AdminError("CLOUDBASE_ADMIN_SAVE_LIMIT", 409)
        self.source_archives = source["source_archives"]
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(self.engine, "begin")
        def begin(connection):
            connection.exec_driver_sql("BEGIN")

        self.metadata = MetaData()
        User.__table__.to_metadata(self.metadata)
        for model in MODELS.values():
            model.__table__.to_metadata(self.metadata)
        self.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        try:
            # These are calculation-only FK identities, not logins. The real
            # CloudBase identity is authoritative; no durable admin flag/password
            # is imported or returned. Only this request's verified actor is admin.
            for actor_id in sorted(self.actors):
                self.db.execute(self.metadata.tables["users"].insert().values(
                    id=actor_id, username=f"cloudbase-actor-{actor_id}",
                    email=f"actor-{actor_id}@cloudbase.invalid",
                    hashed_password="!cloudbase-no-password-login",
                    is_active=True, is_admin=actor_id == self.actor_id, is_verified=False))
            for name, model in MODELS.items():
                table = self.metadata.tables[model.__tablename__]
                for values in source["rows"][name]:
                    if type(values) is not dict or set(values) != set(table.columns.keys()):
                        raise AdminError("CLOUDBASE_ADMIN_ROW_INVALID", 409)
                    row = copy.deepcopy(values)
                    for column in table.columns:
                        if isinstance(column.type, DateTime) and row[column.name] is not None:
                            row[column.name] = datetime.fromisoformat(row[column.name])
                    self.db.execute(table.insert().values(**row))
            self.db.commit()
        except (ValueError, TypeError, IntegrityError):
            self.close()
            raise AdminError("CLOUDBASE_ADMIN_WORKSPACE_INVALID", 409) from None
        except Exception:
            self.close()
            raise
        self.packages = PackageImportService(self.db)
        self.review = ScriptReviewService(self.db)
        self.publication = ScriptPublicationService(self.db)

    @staticmethod
    def empty() -> dict:
        return {"schema_version": WORKSPACE_SCHEMA, "actors": [],
                "rows": {name: [] for name in MODELS}, "source_archives": {}}

    @staticmethod
    def _bounded(payload: dict) -> None:
        try:
            size = len(canonical_json(payload).encode("utf-8"))
        except (ValueError, UnicodeError):
            raise AdminError("CLOUDBASE_ADMIN_WORKSPACE_INVALID", 409) from None
        if size > MAX_WORKSPACE_BYTES:
            raise AdminError("CLOUDBASE_ADMIN_SAVE_LIMIT", 409)

    def snapshot(self) -> dict:
        self.db.commit()
        result = self.empty()
        result["actors"] = sorted(self.actors)
        result["source_archives"] = copy.deepcopy(self.source_archives)
        for name, model in MODELS.items():
            table = self.metadata.tables[model.__tablename__]
            result["rows"][name] = [
                {str(key): value.isoformat() if isinstance(value, datetime) else value
                 for key, value in row.items()}
                for row in self.db.execute(select(table).order_by(table.c.id)).mappings()
            ]
            if len(result["rows"][name]) > 1000:
                raise AdminError("CLOUDBASE_ADMIN_SAVE_LIMIT", 409)
        self._bounded(result)
        return result

    def close(self) -> None:
        self.db.close()
        self.engine.dispose()


class CloudBaseAdmin:
    def __init__(self, gateway: CloudBaseGateway, blob_store: PrivateBlobStore | None = None):
        self.gateway, self.blob_store = gateway, blob_store

    async def read_runtime_manifests(self, *, require_current: bool = True,
                                     release_ids: list[int] | None = None) -> dict:
        """Internal backend projection, NEVER a raw player/admin HTTP response.

        Returns verified formal snapshots plus the PG dependency revision. The
        caller MUST check that dependency in the same PG transaction as creating
        or mutating a new opening/play; a preceding read alone is not a lock.
        Historical reads preserve old snapshots, without claiming current approval.
        PUBLIC_PREVIEW lives in its separate explicit manifest namespace.
        """
        if type(require_current) is not bool or (release_ids is not None and (
            type(release_ids) is not list or len(release_ids) > 100
            or any(type(value) is not int or value <= 0 for value in release_ids))):
            raise AdminError("CLOUDBASE_RUNTIME_RELEASE_REQUEST_INVALID", 422)
        record = await self.gateway.get("admin_workspace", "global", "system")
        if record is None:
            return {"manifests": [], "dependency": None}
        unit = AdminWorkspace(None, record["payload"], _runtime_readonly=True)
        try:
            rows = unit.db.query(ScriptPackageRelease).order_by(ScriptPackageRelease.id).limit(101).all()
            if len(rows) > 100:
                raise AdminError("PUBLICATION_RELEASE_LIMIT_EXCEEDED", 409)
            if release_ids is not None:
                if set(release_ids) - {cast(int, row.id) for row in rows}:
                    raise AdminError("CLOUDBASE_RUNTIME_RELEASE_NOT_FOUND", 404)
                rows = [row for row in rows if row.id in release_ids]
            manifests = []
            for row in rows:
                release_id = cast(int, row.id)
                historical = unit.publication.get_release(release_id, require_current=False)
                if historical.get("publication_mode") == "LOCAL_TRIAL":
                    raise AdminError("CLOUDBASE_LOCAL_TRIAL_NOT_PUBLIC", 409)
                if require_current:
                    def read(store, release_id=release_id):
                        unit.publication.source_store = store
                        return unit.publication.get_release(release_id, require_current=True)
                    try:
                        item = await self._source(unit, historical["bundle_hash"], read)
                    except PublicationConflict as exc:
                        if release_ids is None and exc.code == "PUBLICATION_APPROVAL_EXPIRED":
                            continue
                        raise
                else:
                    item = historical
                manifests.append(item)
            return {"manifests": manifests, "dependency": {
                "kind": "admin_workspace", "key": "global", "owner": "system",
                "version": record["version"], "digest": record["digest"],
            }}
        except PublicationError as exc:
            raise AdminError(exc.code, 409) from None
        except (PackageConflict, PackageNotFound):
            raise AdminError("CLOUDBASE_RUNTIME_RELEASE_INVALID", 409) from None
        except PrivateStorageError as exc:
            raise AdminError(exc.code, exc.status) from None
        finally:
            unit.close()

    async def run(self, actor: dict, operation: str, body: dict | None = None) -> Any:
        require_admin(actor)
        if operation in {"creative.create", "authoring.create", "creative.step", "authoring.step"}:
            # 003 job receipts alone do not constitute a configured compiler/audit
            # adapter. Never create a pretend queued job or model report.
            raise AdminError("CLOUDBASE_MODEL_DISABLED", 503)
        if operation not in _READS | _WRITES:
            raise AdminError("CLOUDBASE_ADMIN_OPERATION_NOT_AVAILABLE", 501)
        return await self._run(actor, operation, {} if body is None else body)

    async def register_source_archive(self, actor: dict, archive: dict, *, idempotency_key: str) -> dict:
        """Server/bootstrap only; no HTTP route. Installs actual private bytes.

        Existing reports cannot be injected this way. Verification creates and
        uploads new reports through the original SourceBundleStore instead.
        """
        require_admin(actor)
        if (type(archive) is not dict or set(archive) != {"bundle_hash", "manifest_ref", "file_refs", "report_refs"}
                or archive["report_refs"] != {} or not isinstance(idempotency_key, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}", idempotency_key)):
            raise AdminError("CLOUDBASE_SOURCE_REGISTRATION_INVALID", 422)
        _digest(archive["bundle_hash"])
        return await self._run(actor, "_install_source", {
            "archive": archive, "idempotency_key": idempotency_key})

    async def _run(self, actor: dict, operation: str, body: dict) -> Any:
        actor_id = require_admin(actor)
        if type(body) is not dict:
            raise AdminError("CLOUDBASE_ADMIN_REQUEST_INVALID", 422)
        try:
            if len(canonical_json(body).encode("utf-8")) > MAX_WORKSPACE_BYTES:
                raise AdminError("CLOUDBASE_ADMIN_REQUEST_TOO_LARGE", 413)
        except (ValueError, UnicodeError):
            raise AdminError("CLOUDBASE_ADMIN_REQUEST_INVALID", 422) from None
        record = await self.gateway.get("admin_workspace", "global", "system")
        readonly = operation in _READS
        if record is None and not readonly:
            try:
                result = await self.gateway.create("admin_workspace", "global", "system", AdminWorkspace.empty(),
                                                   idempotency_key="admin-workspace-init")
                record = result["record"]
            except GatewayConflict:
                record = await self.gateway.get("admin_workspace", "global", "system")
                if record is None:
                    raise AdminError("CLOUDBASE_ADMIN_WRITE_CONFLICT", 409)
        unit = AdminWorkspace(actor, record["payload"] if record else None)
        try:
            result = await self._dispatch(unit, operation, body)
            if readonly:
                return result
            request = body.get("request", body)
            if type(request) is not dict:
                raise AdminError("CLOUDBASE_ADMIN_REQUEST_INVALID", 422)
            idem = request.get("idempotency_key")
            if operation in {"source.verify", "candidate.verify"} and idem is None:
                # Historical verification endpoints have no client key. Recheck
                # bytes before any replay; immutable orphan reports are safe if
                # CAS loses. This never repeats a model/provider call.
                idem = content_hash({"actor": actor_id, "operation": operation, "body": body})
            if not isinstance(idem, str) or not 1 <= len(idem) <= 192:
                raise AdminError("CLOUDBASE_ADMIN_IDEMPOTENCY_REQUIRED", 422)
            if record is None:
                raise AdminError("CLOUDBASE_ADMIN_WRITE_CONFLICT", 409)
            event_payload = {"operation": operation, "actor": actor_id, "response": result}
            if len(canonical_json(event_payload).encode("utf-8")) > 65_536:
                raise AdminError("CLOUDBASE_ADMIN_RECEIPT_TOO_LARGE", 413)
            committed = await self.gateway.cas_append(
                "admin_workspace", "global", "system", record["version"], unit.snapshot(),
                event_payload,
                idempotency_key=content_hash({"actor": actor_id, "operation": operation, "key": idem}),
                request_digest=content_hash({"actor": actor_id, "operation": operation, "body": body}))
            return committed["event"]["response"]
        except GatewayError:
            raise
        except PackageNotFound:
            raise AdminError("CLOUDBASE_ADMIN_RECORD_NOT_FOUND", 404) from None
        except PublicationError as exc:
            code = exc.code if _SAFE_CODE.fullmatch(exc.code) else "CLOUDBASE_PUBLICATION_BLOCKED"
            raise AdminError(code, 409 if isinstance(exc, PublicationConflict) else 422) from None
        except PackageConflict:
            raise AdminError("CLOUDBASE_ADMIN_WRITE_CONFLICT", 409) from None
        except SourceBundleNotFound:
            raise AdminError("CLOUDBASE_SOURCE_NOT_FOUND", 404) from None
        except PrivateStorageError as exc:
            raise AdminError(exc.code, exc.status) from None
        except SourceBundleError:
            raise AdminError("CLOUDBASE_SOURCE_INVALID", 409) from None
        except (ValidationError, PackageInputError, ReviewInputError, RuleReviewError):
            raise AdminError("CLOUDBASE_ADMIN_REQUEST_INVALID", 422) from None
        finally:
            unit.close()

    async def _source(self, unit: AdminWorkspace, bundle_hash: Any, callback, *, persist: bool = False):
        bundle_hash = _digest(bundle_hash)
        archive = unit.source_archives.get(bundle_hash)
        if archive is None:
            raise AdminError("CLOUDBASE_SOURCE_NOT_REGISTERED", 503)
        if self.blob_store is None:
            raise AdminError("CLOUDBASE_SOURCE_STORAGE_NOT_CONFIGURED", 503)
        async with self.blob_store.source_workspace(bundle_hash, archive) as workspace:
            result = callback(workspace.store)
            if persist:
                archive["report_refs"].update(await workspace.persist_reports())
            return result

    async def _publication(self, unit: AdminWorkspace, operation: str, body: dict):
        version = _identifier(body, "version_id")
        raw = body.get("request", {})
        if type(raw) is not dict:
            raise AdminError("CLOUDBASE_ADMIN_REQUEST_INVALID", 422)
        bundle = raw.get("bundle_hash", body.get("bundle_hash"))
        if bundle is not None:
            bundle = _digest(bundle)
        state = unit.publication._collect(version, bundle)
        bundle = state["bundle_hash"]
        if operation == "publication.get":
            call = lambda store: unit.publication.gate_state(version, store, bundle)
        elif operation == "publication.approve":
            parsed = ApprovePublicationRequest.model_validate(raw)
            call = lambda store: unit.publication.approve(version, parsed, unit.actor_id, store)
        else:
            parsed = PublishPackageRequest.model_validate(raw)
            call = lambda store: unit.publication.publish(version, parsed, unit.actor_id, store)
        if bundle is None or bundle not in unit.source_archives or self.blob_store is None:
            # Reading shows a truthful closed gate; approve/publish still run
            # their ORIGINAL full model/manual/basis checks and cannot succeed.
            return call(_UnavailableSourceStore())
        return await self._source(unit, bundle, call, persist=operation != "publication.get")

    async def _dispatch(self, unit: AdminWorkspace, operation: str, body: dict):
        if operation == "candidate.list":
            return unit.review.list_candidates()
        if operation == "candidate.get":
            return unit.packages.get_version(_identifier(body, "version_id"))
        if operation == "candidate.check":
            return validate_package(unit.packages.get_version(_identifier(body, "version_id"))["package"])
        if operation == "candidate.import":
            parsed = ImportPackageRequest.model_validate(body)
            return unit.packages.submit(parsed.package, submitted_by=unit.actor_id, idempotency_key=parsed.idempotency_key)
        if operation == "import.get":
            return unit.packages.get_job(_identifier(body, "job_id"))
        if operation == "review.get":
            return unit.review.get_review(_identifier(body, "version_id"))
        if operation == "finding.dispose":
            return unit.review.add_disposition(_identifier(body, "audit_id"),
                FindingDispositionRequest.model_validate(body.get("request")), unit.actor_id)
        if operation.startswith("publication."):
            return await self._publication(unit, operation, body)
        if operation == "manual_audit.create":
            version = _identifier(body, "version_id")
            parsed = parse_submit_audit_request(body.get("request"))
            existing, document = unit.review.prepare_audit(version, parsed, unit.actor_id)
            if existing is not None:
                return existing
            return await self._source(unit, parsed.bundle_hash,
                lambda store: unit.review.save_audit(version, parsed, unit.actor_id,
                    store.verify(parsed.bundle_hash, document=document), store), persist=True)
        if operation == "candidate.verify":
            document = unit.packages.get_version(_identifier(body, "version_id"))["package"]
            return await self._source(unit, body.get("bundle_hash"),
                lambda store: store.verify(body["bundle_hash"], document=document), persist=True)
        if operation == "rule_review.get":
            version = unit.packages.get_version(_identifier(body, "version_id"))
            if version["package_hash"] != body.get("expected_package_hash"):
                raise PackageConflict("候选已变化")
            return await self._source(unit, body.get("bundle_hash"), lambda store: build_rule_review(
                version["package"], body["bundle_hash"], store, offset=body.get("offset", 0), limit=body.get("limit", 20)))
        if operation == "_install_source":
            archive = copy.deepcopy(body["archive"])
            bundle = archive["bundle_hash"]
            if bundle in unit.source_archives:
                prior = unit.source_archives[bundle]
                if any(prior.get(key) != archive[key] for key in ("bundle_hash", "manifest_ref", "file_refs")):
                    raise AdminError("CLOUDBASE_SOURCE_ALREADY_REGISTERED", 409)
            else:
                unit.source_archives[bundle] = archive
            return await self._source(unit, bundle, lambda store: store.describe(bundle))
        if operation == "source.list":
            return [await self._source(unit, bundle, lambda store, key=bundle: store.describe(key, include_sources=False))
                    for bundle in sorted(unit.source_archives)]
        if operation == "source.get":
            return await self._source(unit, body.get("bundle_hash"), lambda store: store.describe(body["bundle_hash"]))
        if operation == "source.read":
            return await self._source(unit, body.get("bundle_hash"),
                lambda store: store.read_source(body["bundle_hash"], body.get("source_id")))
        if operation == "source.verify":
            return await self._source(unit, body.get("bundle_hash"),
                lambda store: store.verify(body["bundle_hash"]), persist=True)
        if operation == "source.report":
            report = _digest(body.get("report_hash"))
            bundle = next((key for key, archive in unit.source_archives.items() if report in archive["report_refs"]), None)
            if bundle is None:
                raise AdminError("CLOUDBASE_SOURCE_REPORT_NOT_FOUND", 404)
            return await self._source(unit, bundle, lambda store: store.get_report(report))
        raise AdminError("CLOUDBASE_ADMIN_OPERATION_NOT_AVAILABLE", 501)
