"""Explicit local-only trial releases, distinct from formal reviewed publication."""
from __future__ import annotations

import ipaddress
import os

from src.db.models.script_publication import ScriptPackageRelease
from src.fusion.script_publication import (
    LOCAL_TRIAL_POLICY_VERSION, PublicationConflict, ScriptPublicationService,
)


TRIAL_NOTICE = "本地试玩版，尚未完成模型审核。可阅读私本、搜证和揭晓结局；实时 AI 对话未启用。"


def local_trials_enabled() -> bool:
    return (os.getenv("ENABLE_LOCAL_TRIALS", "").lower() == "true"
            and os.getenv("ENV", "development").lower() in {"development", "dev", "local", "test"})


def loopback_request(request) -> bool:
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


class LocalTrialPublicationService(ScriptPublicationService):
    @property
    def policy_version(self) -> str:
        return LOCAL_TRIAL_POLICY_VERSION

    def _collect(self, version_id: int, bundle_hash: str | None = None) -> dict:
        if not local_trials_enabled():
            raise PublicationConflict("LOCAL_TRIAL_DISABLED")
        if bundle_hash is None:
            approval = self._latest_approval(version_id)
            if approval and approval["policy_version"] == self.policy_version:
                bundle_hash = approval["bundle_hash"]
        state = super()._collect(version_id, bundle_hash)
        # A trial neither creates a model report nor claims a formal human audit.
        # Existing findings, unknown calls, source checks and bindings still apply.
        state["checks"] = [c for c in state["checks"] if c["code"] not in {
            "MODEL_AUDIT_COMPLETE", "MANUAL_AUDIT_COMPLETE",
        }]
        state["checks"].append({"code": "LOCAL_TRIAL_CONTRACT", "passed":
            state["version"]["package"]["schema_version"] in {"script-package/1.1", "script-package/1.2"},
            "message": "仅用于本机基础文字试玩，未声明模型或正式人工审核通过"})
        return state

    def get_release(self, release_id: int, require_current: bool = True) -> dict:
        if not local_trials_enabled():
            raise PublicationConflict("LOCAL_TRIAL_DISABLED")
        result = super().get_release(release_id, require_current)
        if result.get("publication_mode") != "LOCAL_TRIAL":
            raise PublicationConflict("LOCAL_TRIAL_SCOPE_MISMATCH")
        return result


class RuntimePublicationReader:
    """Route trial reads only for enabled loopback requests; formal gates stay strict."""
    def __init__(self, db, source_store=None, *, allow_local_trials: bool = False):
        self.db = db
        self.source_store = source_store
        self.allow_local_trials = allow_local_trials
        self.formal = ScriptPublicationService(db, source_store)
        self.trial = LocalTrialPublicationService(db, source_store)

    @property
    def local_trials_enabled(self) -> bool:
        return self.allow_local_trials and local_trials_enabled()

    def get_release(self, release_id: int, require_current: bool = True) -> dict:
        # Determine policy from immutable records, never from a client flag.
        result = self.formal.get_release(release_id, require_current=False)
        if result.get("publication_mode") == "LOCAL_TRIAL":
            if not self.local_trials_enabled:
                raise PublicationConflict("LOCAL_TRIAL_DISABLED")
            return self.trial.get_release(release_id, require_current)
        return self.formal.get_release(release_id, require_current)

    def list_releases(self) -> list[dict]:
        result = self.formal.list_releases()
        if self.local_trials_enabled:
            result.extend(row for row in self.trial.list_releases() if row.get("publication_mode") == "LOCAL_TRIAL")
        return sorted(result, key=lambda row: row["id"], reverse=True)

    def presentation(self, release_id: int) -> dict:
        row = self.db.get(ScriptPackageRelease, release_id)
        if row is None:
            return {}
        release = self.get_release(release_id, require_current=False)
        return trial_presentation(release)


def trial_presentation(release: dict) -> dict:
    if release.get("publication_mode") != "LOCAL_TRIAL":
        return {}
    return {"local_trial": {"label": "本地试玩版", "notice": TRIAL_NOTICE,
                            "model_audit_complete": False, "ai_dialogue_available": False}}


def runtime_publication_reader(db, request, source_store=None):
    return RuntimePublicationReader(db, source_store, allow_local_trials=loopback_request(request))
