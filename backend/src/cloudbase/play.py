"""CloudBase persistence for the existing deterministic game engine.

SQLite is an ephemeral unit of work, never this deployment's durable database.
Each command restores only one authenticated player's bounded workspace, runs
the existing rule engine, then atomically commits the workspace to PostgreSQL
through an expected-version RPC. No network/model calls run inside this unit.
An uncertain response is repeated with the same input digest and receipt key.
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
import re
from typing import Any, Protocol, cast

from sqlalchemy import (CheckConstraint, Column, ForeignKey, Integer, MetaData,
                        Table, UniqueConstraint, create_engine, event, select)
from sqlalchemy.orm import Session

from src.cloudbase.gateway import CloudBaseGateway, GatewayConflict, GatewayError
from src.db.models.package_runtime import ScriptPackagePlaySession
from src.db.models.package_play import ScriptPackagePlay, ScriptPackagePlayEvent
from src.fusion.budget import BudgetPolicy
from src.fusion.package_play import PackagePlayService, PackagePlayError
from src.fusion.package_runtime import PackageRuntimeService, PackageRuntimeError
from src.fusion.package_role_model import PackageRoleModel
from src.fusion.package_validation import canonical_json, content_hash, validate_package
from src.fusion.agents import PlayerModelSettings


WORKSPACE_SCHEMA = "cloudbase-play-workspace/1.0"
PREVIEW_SCHEMA = "cloudbase-public-preview/1.0"
PUBLISHED_SCHEMA = "cloudbase-published-runtime/1.0"
FORMAL_ID_OFFSET = 1_000_000
PREVIEW_NOTICE = "公开试玩剧本，尚未完成正式模型审核。可搜证和体验结局；实时 AI 对话未开启。"
ORIGINAL_PACKAGE_HASH = "1ceebcca9097198ff90eeb7d439c6b6dd5d5f5c5fd8502c34bbee65bd4fc6497"
MAX_PLAYS = 20
MAX_EVENTS = 3000
MAX_WORKSPACE_BYTES = 900_000
MODELS = {
    "openings": ScriptPackagePlaySession,
    "plays": ScriptPackagePlay,
    "events": ScriptPackagePlayEvent,
}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class PublicationAuthority(Protocol):
    async def read_runtime_manifests(self, *, require_current: bool = True,
                                     release_ids: list[int] | None = None) -> dict: ...


def original_preview(package: dict) -> dict:
    """Explicit test artifact, with no manufactured approval or formal release."""
    if content_hash(package) != ORIGINAL_PACKAGE_HASH or not validate_package(package)["valid"]:
        raise ValueError("CLOUDBASE_ORIGINAL_PREVIEW_INVALID")
    basis = {"schema_version": PREVIEW_SCHEMA, "id": 1, "version_id": 1,
             "package_hash": ORIGINAL_PACKAGE_HASH, "publication_mode": "PUBLIC_PREVIEW",
             "model_audit_complete": False, "manual_audit_complete": False}
    return {**basis, "release_hash": content_hash(basis), "package": copy.deepcopy(package)}


def published_runtime(release: dict) -> dict:
    """Map a server-verified publication snapshot to a separate runtime ID space.

    This is a reference binding, not an approval builder. Its input must come
    from CloudBaseAdmin.read_runtime_manifests, which invokes the ORIGINAL
    publication verifier. Static/client manifests cannot enter this path.
    Current validity is deliberately not frozen into the immutable reference.
    """
    if (type(release) is not dict or release.get("publication_mode") not in (None, "FORMAL")
            or any(type(release.get(key)) is not int or not 1 <= release[key] <= 2_147_483_647 - FORMAL_ID_OFFSET
                   for key in ("id", "version_id"))
            or any(not isinstance(release.get(key), str) or not _DIGEST.fullmatch(release[key])
                   for key in ("release_hash", "package_hash", "approval_hash", "basis_hash", "bundle_hash", "source_report_hash"))
            or type(release.get("approval_id")) is not int or release["approval_id"] <= 0
            or type(release.get("package")) is not dict
            or content_hash(release["package"]) != release["package_hash"]
            or not validate_package(release["package"])["valid"]):
        raise ValueError("CLOUDBASE_FORMAL_MANIFEST_INVALID")
    basis = {"schema_version": PUBLISHED_SCHEMA, "publication_mode": "FORMAL",
             "id": FORMAL_ID_OFFSET + release["id"], "version_id": FORMAL_ID_OFFSET + release["version_id"],
             "source_release_id": release["id"], "source_version_id": release["version_id"],
             "source_release_hash": release["release_hash"], "package_hash": release["package_hash"]}
    return {**basis, "release_hash": content_hash(basis), "package": copy.deepcopy(release["package"])}


class ManifestReader:
    """Only deployment-installed manifests are accepted, never a player package."""
    def __init__(self, manifests: list[dict], *, formal_history: list[dict] | None = None,
                 current_formal_ids: set[int] | None = None):
        self.manifests: dict[int, dict] = {}
        self.current: set[int] = set()
        for raw in manifests:
            item = copy.deepcopy(raw)
            if item.get("schema_version") == PREVIEW_SCHEMA:
                expected = original_preview(item["package"])
                if item != expected:
                    raise ValueError("CLOUDBASE_PREVIEW_BASIS_INVALID")
            else:
                # Public/static input remains preview-only. Formal snapshots
                # arrive separately from the live server publication authority.
                raise ValueError("CLOUDBASE_MANIFEST_POLICY_UNSUPPORTED")
            if item["id"] in self.manifests:
                raise ValueError("CLOUDBASE_MANIFEST_DUPLICATE")
            self.manifests[item["id"]] = item
            self.current.add(item["id"])
        for raw in formal_history or []:
            item = published_runtime(raw)
            previous = self.manifests.get(item["id"])
            if previous is not None and previous != item:
                raise ValueError("CLOUDBASE_FORMAL_MANIFEST_CHANGED")
            self.manifests[item["id"]] = item
        for identifier in current_formal_ids or set():
            external = FORMAL_ID_OFFSET + identifier
            if type(identifier) is not int or identifier <= 0 or external not in self.manifests:
                raise ValueError("CLOUDBASE_FORMAL_CURRENT_INVALID")
            self.current.add(external)

    def list_releases(self):
        return [copy.deepcopy(item) for identifier, item in self.manifests.items() if identifier in self.current]

    def historical_releases(self):
        return [copy.deepcopy(item) for item in self.manifests.values()]

    def get_release(self, release_id: int, *, require_current: bool = True):
        if (type(release_id) is not int or release_id not in self.manifests
                or (require_current and release_id not in self.current)):
            raise ValueError("CLOUDBASE_MANIFEST_NOT_FOUND")
        return copy.deepcopy(self.manifests[release_id])

    def presentation(self, release_id: int) -> dict:
        release = self.get_release(release_id, require_current=False)
        if release["schema_version"] == PUBLISHED_SCHEMA:
            return {}
        return {"public_preview": {"label": "公开试玩版", "notice": PREVIEW_NOTICE,
                                  "model_audit_complete": False, "ai_dialogue_available": False}}


class CloudRuntime(PackageRuntimeService):
    def __init__(self, db: Session, publisher: ManifestReader):
        super().__init__(db, publisher)
        self.manifest_reader = publisher

    def _package(self, release: dict) -> dict:
        # Unlike the normal deployment, versions are immutable manifest
        # objects, not SQLAlchemy rows. All normal snapshot/role checks remain.
        expected = self.publisher.get_release(release["id"], require_current=False)
        if expected != release or content_hash(expected["package"]) != expected["package_hash"]:
            raise PackageRuntimeError("PACKAGE_RUNTIME_SNAPSHOT_INVALID")
        return copy.deepcopy(expected["package"])

    @staticmethod
    def _catalog(package: dict, release: dict) -> dict:
        result = PackageRuntimeService._catalog(package, release)
        if release["schema_version"] == PUBLISHED_SCHEMA:
            return result
        return {**result,
                "public_preview": {"label": "公开试玩版", "notice": PREVIEW_NOTICE,
                                   "model_audit_complete": False, "ai_dialogue_available": False}}

    def _opening_view(self, row, package):
        return {**super()._opening_view(row, package),
                **self.manifest_reader.presentation(cast(int, row.release_id))}


def _metadata() -> MetaData:
    """Build a local calculation schema with truthful manifest foreign keys.

    Do not mutate the normal ORM metadata or insert fake release/review rows.
    The physical calculation tables have the same columns used by the engine,
    but refer to CloudBase actors/manifests rather than formal releases.
    """
    metadata = MetaData()
    Table("cloudbase_actors", metadata, Column("id", Integer, primary_key=True))
    Table("cloudbase_manifests", metadata,
          Column("id", Integer, primary_key=True),
          Column("version_id", Integer, nullable=False, unique=True))
    for model in MODELS.values():
        original = model.__table__
        columns = []
        for col in original.columns:
            fk = None
            if col.name == "owner_user_id":
                fk = "cloudbase_actors.id"
            elif col.name == "release_id":
                fk = "cloudbase_manifests.id"
            elif col.name == "version_id":
                fk = "cloudbase_manifests.version_id"
            elif col.name == "opening_session_id":
                fk = "script_package_play_sessions.session_id"
            elif col.name == "play_id" and model is ScriptPackagePlayEvent:
                fk = "script_package_plays.play_id"
            columns.append(Column(col.name, copy.copy(col.type),
                                  *([ForeignKey(fk)] if fk else []),
                                  primary_key=col.primary_key, nullable=col.nullable))
        constraints = []
        for constraint in original.constraints:
            if isinstance(constraint, UniqueConstraint):
                constraints.append(UniqueConstraint(*[col.name for col in constraint.columns]))
            elif isinstance(constraint, CheckConstraint):
                constraints.append(CheckConstraint(str(constraint.sqltext)))
        Table(original.name, metadata, *columns, *constraints)
    return metadata


class Workspace:
    def __init__(self, owner: int, payload: dict | None, manifests: list[dict], *,
                 formal_history: list[dict] | None = None, current_formal_ids: set[int] | None = None):
        if type(owner) is not int or owner <= 0:
            raise ValueError("CLOUDBASE_PLAYER_REQUIRED")
        self.owner = owner
        self.reader = ManifestReader(manifests, formal_history=formal_history, current_formal_ids=current_formal_ids)
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.metadata = _metadata()
        self.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.execute(self.metadata.tables["cloudbase_actors"].insert().values(id=owner))
        for manifest in self.reader.historical_releases():
            self.db.execute(self.metadata.tables["cloudbase_manifests"].insert().values(
                id=manifest["id"], version_id=manifest["version_id"]))
        source = payload if payload is not None else self.empty(owner)
        if (type(source) is not dict or set(source) != {"schema_version", "owner", *MODELS}
                or source["schema_version"] != WORKSPACE_SCHEMA or source["owner"] != owner
                or any(type(source[name]) is not list for name in MODELS)):
            self.close()
            raise ValueError("CLOUDBASE_WORKSPACE_INVALID")
        if len(canonical_json(source).encode()) > MAX_WORKSPACE_BYTES:
            self.close()
            raise ValueError("CLOUDBASE_WORKSPACE_TOO_LARGE")
        if len(source["plays"]) > MAX_PLAYS or len(source["events"]) > MAX_EVENTS:
            self.close()
            raise ValueError("CLOUDBASE_WORKSPACE_LIMIT")
        for name, model in MODELS.items():
            table = self.metadata.tables[model.__tablename__]
            for values in source[name]:
                if type(values) is not dict or set(values) != set(table.columns.keys()):
                    self.close()
                    raise ValueError("CLOUDBASE_WORKSPACE_ROW_INVALID")
                row = copy.deepcopy(values)
                if name != "events" and row["owner_user_id"] != owner:
                    self.close()
                    raise ValueError("CLOUDBASE_WORKSPACE_OWNER_INVALID")
                for field in ("created_at", "updated_at"):
                    row[field] = datetime.fromisoformat(row[field])
                self.db.execute(table.insert().values(**row))
        self.db.commit()
        # An unsupported, fixed provider deliberately has no profile, endpoint
        # or key lookup. The free rules runtime must not inherit model settings
        # or paid-call switches from the legacy deployment environment.
        model = PackageRoleModel(settings=PlayerModelSettings(
            provider="cloudbase-disabled", model="", timeout_seconds=1,
            retries=0, max_output_tokens=64, max_input_bytes=1024,
            thinking_mode="disabled", temperature=0, paid_calls_enabled=False))
        policy = BudgetPolicy(0, None, Decimal(0), Decimal(0), Decimal(0), False,
                              pricing_version="cloudbase-free-preview/1.0")
        self.runtime = CloudRuntime(self.db, self.reader)
        self.play = PackagePlayService(self.db, self.reader, model=model, policy=policy)
        self.play.runtime = self.runtime

    @staticmethod
    def empty(owner: int) -> dict:
        return {"schema_version": WORKSPACE_SCHEMA, "owner": owner,
                "openings": [], "plays": [], "events": []}

    def snapshot(self) -> dict:
        self.db.commit()
        result = self.empty(self.owner)
        for name, model in MODELS.items():
            table = self.metadata.tables[model.__tablename__]
            result[name] = [{str(key): value.isoformat() if isinstance(value, datetime) else value
                             for key, value in row.items()}
                            for row in self.db.execute(select(table).order_by(table.c.id)).mappings()]
        if (len(result["plays"]) > MAX_PLAYS or len(result["events"]) > MAX_EVENTS
                or len(canonical_json(result).encode()) > MAX_WORKSPACE_BYTES):
            raise PackagePlayError("CLOUDBASE_FREE_SAVE_LIMIT", 409)
        return result

    def close(self):
        self.db.close()
        self.engine.dispose()

    def dispatch(self, operation: str, body: dict) -> Any:
        if operation == "catalog":
            return self.runtime.list_releases(self.owner)
        if operation == "opening.create":
            return self.runtime.create(body, self.owner)
        if operation == "opening.get":
            return self.runtime.get(body["session_id"], self.owner)
        if operation == "play.create":
            return self.play.create(body, self.owner)
        if operation == "play.get":
            return self.play.get(body["play_id"], self.owner)
        if operation == "play.find":
            return self.play.find_for_opening(body["opening_session_id"], self.owner)
        if operation == "play.library":
            return self.play.list_library(self.owner, body.get("offset", 0), body.get("limit", 20))
        if operation in ("play.act", "play.speak"):
            method = self.play.act if operation == "play.act" else self.play.speak
            return method(body["play_id"], body["command"], self.owner)
        raise PackagePlayError("CLOUDBASE_OPERATION_NOT_AVAILABLE", 404)


class CloudBasePlay:
    def __init__(self, gateway: CloudBaseGateway, manifests: list[dict], *,
                 authority: PublicationAuthority | None = None):
        self.gateway = gateway
        self.manifests = copy.deepcopy(manifests)
        self.authority = authority
        ManifestReader(manifests)

    @staticmethod
    def _history_ids(payload: dict | None) -> list[int]:
        identifiers: set[int] = set()
        if payload is not None:
            if type(payload) is not dict:
                raise PackagePlayError("CLOUDBASE_WORKSPACE_INVALID")
            for collection in ("openings", "plays"):
                rows = payload.get(collection)
                if type(rows) is not list:
                    raise PackagePlayError("CLOUDBASE_WORKSPACE_INVALID")
                for row in rows:
                    identifier = row.get("release_id") if type(row) is dict else None
                    if type(identifier) is not int or identifier <= 0:
                        raise PackagePlayError("CLOUDBASE_WORKSPACE_INVALID")
                    if identifier >= FORMAL_ID_OFFSET:
                        identifiers.add(identifier - FORMAL_ID_OFFSET)
        if len(identifiers) > 100 or any(value <= 0 for value in identifiers):
            raise PackagePlayError("CLOUDBASE_WORKSPACE_RELEASE_LIMIT")
        return sorted(identifiers)

    @staticmethod
    def _authority_result(result: dict, *, current: bool) -> tuple[list[dict], dict | None]:
        if (type(result) is not dict or type(result.get("manifests")) is not list
                or len(result["manifests"]) > 100):
            raise GatewayError("CLOUDBASE_PUBLICATION_AUTHORITY_INVALID", 503)
        dependency = result.get("dependency")
        if dependency is not None and (
                type(dependency) is not dict or set(dependency) != {"kind", "key", "owner", "version", "digest"}
                or (dependency["kind"], dependency["key"], dependency["owner"]) != ("admin_workspace", "global", "system")
                or type(dependency["version"]) is not int or dependency["version"] <= 0
                or not isinstance(dependency["digest"], str) or not _DIGEST.fullmatch(dependency["digest"])):
            raise GatewayError("CLOUDBASE_PUBLICATION_AUTHORITY_INVALID", 503)
        if result["manifests"] and dependency is None:
            raise GatewayError("CLOUDBASE_PUBLICATION_AUTHORITY_INVALID", 503)
        for release in result["manifests"]:
            try:
                published_runtime(release)
            except (ValueError, TypeError, KeyError):
                raise GatewayError("CLOUDBASE_FORMAL_MANIFEST_INVALID", 503) from None
            if current and release.get("current_approval_valid") is not True:
                raise GatewayError("CLOUDBASE_FORMAL_APPROVAL_INVALID", 503)
        return result["manifests"], dependency

    async def _publication_scope(self, payload: dict | None, operation: str,
                                 readonly: bool) -> tuple[list[dict], set[int], list[dict]]:
        identifiers = self._history_ids(payload)
        authority = self.authority  # fixed for this operation, not cached approval
        if authority is None:
            if identifiers:
                raise PackagePlayError("CLOUDBASE_PUBLICATION_AUTHORITY_REQUIRED", 503)
            return [], set(), []
        if operation == "catalog":
            current, dependency = self._authority_result(
                await authority.read_runtime_manifests(require_current=True), current=True)
            return current, {item["id"] for item in current}, []
        history, dependency = self._authority_result(
            await authority.read_runtime_manifests(require_current=False, release_ids=identifiers), current=False)
        if {item["id"] for item in history} != set(identifiers):
            raise GatewayError("CLOUDBASE_FORMAL_HISTORY_SCOPE_INVALID", 503)
        current, current_ids = [], set()
        if not readonly:
            current, current_dependency = self._authority_result(
                await authority.read_runtime_manifests(require_current=True), current=True)
            if current_dependency != dependency:
                raise GatewayConflict("CLOUDBASE_PUBLICATION_AUTHORITY_CHANGED")
            current_ids = {item["id"] for item in current}
        guards = [] if dependency is None else [{
            "kind": dependency["kind"], "key": dependency["key"], "owner": dependency["owner"],
            "expected_version": dependency["version"], "digest": dependency["digest"],
        }]
        return history + current, current_ids, guards

    async def catalog(self) -> list[dict]:
        """Public metadata only: no identity, player state, actor or local DB."""
        history, current_ids, _ = await self._publication_scope(None, "catalog", True)
        reader = ManifestReader(self.manifests, formal_history=history, current_formal_ids=current_ids)
        return [CloudRuntime._catalog(item["package"], item) for item in reader.list_releases()]

    async def run(self, owner: int, operation: str, body: dict) -> Any:
        if type(owner) is not int or owner <= 0:
            raise PackagePlayError("PACKAGE_PLAY_IDENTITY_REQUIRED", 401)
        readonly = operation in {"catalog", "opening.get", "play.get", "play.find", "play.library"}
        if operation == "catalog":
            return await self.catalog()
        key = f"cb-user:{owner}"
        record = await self.gateway.get("play_workspace", key, key)
        if record is None and not readonly:
            try:
                created = await self.gateway.create(
                    "play_workspace", key, key, Workspace.empty(owner), idempotency_key="workspace-init")
                record = created["record"]
            except GatewayConflict:
                record = await self.gateway.get("play_workspace", key, key)
        payload = record["payload"] if record else None
        history, current_ids, guards = await self._publication_scope(payload, operation, readonly)
        unit = Workspace(owner, payload, self.manifests, formal_history=history, current_formal_ids=current_ids)
        try:
            response = unit.dispatch(operation, body)
            if readonly:
                return response
            if record is None:
                # A conflicting initialization followed by a missing read must
                # never claim that the in-memory calculation was persisted.
                raise GatewayConflict("CLOUDBASE_WORKSPACE_NOT_READY")
            command = body.get("command", body)
            idem = command.get("idempotency_key")
            if type(idem) is not str:
                raise PackagePlayError("PACKAGE_PLAY_REQUEST_INVALID", 422)
            digest = content_hash({"operation": operation, "body": body, "owner": owner})
            result = await self.gateway.cas_append_guarded(
                "play_workspace", key, key, record["version"], unit.snapshot(),
                {"operation": operation, "response": response},
                idempotency_key=content_hash({"operation": operation, "key": idem}),
                request_digest=digest, guards=guards)
            return result["event"]["response"]
        finally:
            unit.close()
