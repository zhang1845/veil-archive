"""为本地开发导入一份虚构示例剧本，走完整真实发布链路。

这是「开发者体验」的一部分：让 clone 仓库的开发者能跑通
「导入 → 编译 → 模型审核 → 人工审核 → 审批 → 发布 → /play 可玩」的完整链路。

安全边界（务必理解后再用）：
- 剧本正文是独立原创的合成资料，不来自任何商业剧本。
- 编译与审核会真实调用云模型（默认阿里百炼），需要 .env 里配置好
  DASHSCOPE_API_KEY 与相关定价变量；`--allow-paid` 必须显式传入，否则拒绝执行。
- 人工审核记录由本脚本代填（0 findings、五维覆盖）。这只适用于
  本地开发演示；它不代表真实人工审核，也不改变生产发布门禁。
- 幂等：同一 script_key + content_version 重复执行不会重复发布，
  只返回已有的发布记录。

用法（仓库根目录）：
    backend/.venv/bin/python backend/scripts/seed_demo_package.py \
        --allow-paid --provider aliyun_bailian
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT.parent / "private-data" / "demo-seed"
DEMO_IDEMPOTENCY_KEY = "demo-gallery-repair-v1"
DEMO_CONTENT_VERSION = "demo-v1"
DEMO_TITLE = "虚构展馆维修记录核对（演示）"


class DemoSeedError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require_development_env() -> None:
    """演示种子只允许在开发环境写入主库；生产环境直接拒绝。"""
    env = os.getenv("ENV", "development").lower()
    if env in {"production", "prod"}:
        raise DemoSeedError("DEMO_SEED_PRODUCTION_FORBIDDEN")


def _private_dir(base: Path) -> Path:
    base = Path(base).absolute()
    if base.is_symlink() or base.resolve().is_relative_to(REPOSITORY_ROOT.resolve()):
        raise DemoSeedError("DEMO_SEED_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not base.is_dir() or base.stat().st_mode & 0o077:
        raise DemoSeedError("DEMO_SEED_DIRECTORY_MUST_BE_PRIVATE")
    return base


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导入一份虚构示例剧本并走完整真实发布链路（本地开发专用）。")
    parser.add_argument("--allow-paid", action="store_true",
                        help="显式允许编译/审核可能产生云模型费用；无自动重试。")
    parser.add_argument("--provider", choices=("aliyun_bailian", "volcengine_ark"),
                        default="aliyun_bailian", help="云模型渠道（默认阿里百炼）。")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help="来源快照私有目录；拒绝 Git 仓库内部。")
    return parser


async def run_demo(args: argparse.Namespace) -> dict:
    from src.core.environment import load_project_environment
    load_project_environment()
    _require_development_env()

    from src.db.session import db_manager
    from src.fusion.authoring_jobs import AuthoringJobStore
    from src.fusion.authoring_runner import AuthoringRunner, submit_authoring_job
    from src.fusion.package_import import PackageImportService
    from src.fusion.provider_config import load_selected_config
    from src.fusion.script_publication import ScriptPublicationService
    from src.fusion.script_review import ScriptReviewService
    from src.schemas.script_publication import (
        ApprovePublicationRequest, PublishPackageRequest,
    )
    from src.schemas.script_review import SubmitAuditRequest

    # 复用合成剧本来源与带额度的模型封装（与冒烟验收同一份虚构资料）。
    from scripts.real_authoring_smoke import (
        FIXTURE_ID, make_synthetic_bundle, SmokeAuthoringModel,
    )

    root = _private_dir(args.output_root)
    store, request = make_synthetic_bundle(root)
    # 用演示专属标识，避免与冒烟验收的幂等键冲突。
    request = request.model_copy(update={
        "idempotency_key": DEMO_IDEMPOTENCY_KEY,
        "title": DEMO_TITLE,
        "content_version": DEMO_CONTENT_VERSION,
    })

    config = load_selected_config(args.provider)
    model = SmokeAuthoringModel(config)

    db_manager.initialize()
    try:
        jobs = AuthoringJobStore(db_manager.get_session)
        submitted = await submit_authoring_job(jobs, store, model, request, _demo_actor_id(db_manager, FIXTURE_ID))
        result = await AuthoringRunner(jobs, store, model).run(submitted["id"], allow_paid=args.allow_paid)
        if result["state"] != "COMPLETED":
            raise DemoSeedError(result["error_code"] or "DEMO_SEED_WORKFLOW_INCOMPLETE")
        version_id = result["candidate_version_id"]
        source_report_hash = result["source_report_hash"]

        # 从主库读取候选包；来源核验报告复用同一来源存储。
        with db_manager.session_scope() as db:
            candidate = PackageImportService(db).get_version(version_id)
        package = candidate["package"]
        verification = store.get_report(source_report_hash)

        actor = _demo_actor_id(db_manager, FIXTURE_ID)

        # 人工审核、审批、发布必须在同一个会提交的事务里推进，
        # 后续步骤读取前一步写入的记录。
        with db_manager.session_scope() as db:
            publications = ScriptPublicationService(db, store)
            reviews = ScriptReviewService(db)
            gate = publications.gate_state(version_id, store)

            # 若已发布过（幂等重跑），直接返回。
            if gate["release"] is not None:
                return {"status": "ALREADY_RELEASED", "release": gate["release"],
                        "version_id": version_id, "title": package["title"]}

            # 1) 人工审核：五维覆盖、零发现。演示脚本代填，不代表真实人工审核。
            manual_report = {
                "schema_version": "script-audit/1.1",
                "summary": "演示脚本代填的本地开发审核；五维已覆盖，未发现阻断或警告。",
                "coverage": ["PROVENANCE", "TIMELINE", "EVIDENCE", "KNOWLEDGE_BOUNDARY", "PLAYABILITY"],
                "findings": [],
            }
            audit_body = SubmitAuditRequest(
                idempotency_key=f"{DEMO_IDEMPOTENCY_KEY}-manual",
                expected_package_hash=candidate["package_hash"],
                bundle_hash=verification["bundle_hash"],
                report=manual_report,
            )
            reviews.save_audit(version_id, audit_body, actor, verification, store)

            # 重新读取门禁，拿最新 basis_hash 与模型发现清单。
            gate = publications.gate_state(version_id, store)
            if not gate["can_approve"]:
                failed = [c for c in gate["checks"] if not c["passed"]]
                raise DemoSeedError(
                    "DEMO_SEED_GATE_BLOCKED:" + ",".join(c["code"] for c in failed))

            # 2) 审批：处置全部模型发现（演示剧本通常零发现，列表为空）。
            # 模型发现清单来自 model_reports（与 _decisions 校验的 required 一致）。
            model_dispositions = [
                {"job_id": report["job_id"], "finding_id": finding["id"],
                 "status": "DISMISSED" if finding["severity"] == "BLOCKER" else "ACKNOWLEDGED",
                 "note": "演示脚本代填处置（本地开发）"}
                for report in gate["model_reports"] if report["report"]
                for finding in report["report"]["findings"]
                if finding["severity"] in {"WARNING", "BLOCKER"}
            ]
            approve_body = ApprovePublicationRequest(
                idempotency_key=f"{DEMO_IDEMPOTENCY_KEY}-approve",
                expected_package_hash=candidate["package_hash"],
                bundle_hash=gate["bundle_hash"],
                expected_basis_hash=gate["basis_hash"],
                model_dispositions=model_dispositions,
                note="演示脚本代填审批（本地开发）",
            )
            approval = publications.approve(version_id, approve_body, actor, store)

            # 3) 发布。
            gate = publications.gate_state(version_id, store)
            publish_body = PublishPackageRequest(
                idempotency_key=f"{DEMO_IDEMPOTENCY_KEY}-publish",
                approval_id=approval["id"],
                expected_approval_hash=approval["approval_hash"],
                expected_basis_hash=gate["basis_hash"],
            )
            release = publications.publish(version_id, publish_body, actor, store)
            return {"status": "RELEASED", "release": release, "version_id": version_id,
                    "title": package["title"], "charged_cost_cny": result["charged_cost_cny"]}
    finally:
        db_manager.close()


def _demo_actor_id(db_manager, username: str) -> int:
    """复用或创建演示管理员账户，返回其 id。"""
    from src.db.models import User
    with db_manager.session_scope() as db:
        row = db.query(User).filter_by(username=username).one_or_none()
        if row is not None:
            return row.id
        actor = User(username=username, email=f"{username}@example.invalid",
                     hashed_password="LOGIN_DISABLED_DEMO_FIXTURE",
                     is_active=True, is_admin=True, is_verified=False)
        db.add(actor)
        db.flush()
        return actor.id


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_paid:
        print("错误：编译/审核会产生云模型费用，必须显式传入 --allow-paid。", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(run_demo(args))
    except DemoSeedError as error:
        print(f"演示导入失败：{error.code}", file=sys.stderr)
        return 3
    except Exception as error:  # 不打印私密提示词/凭据/异常文本
        code = getattr(error, "code", None)
        if not isinstance(code, str) or len(code) > 120:
            code = "DEMO_SEED_FAILED"
        print(f"演示导入失败：{code}", file=sys.stderr)
        return 3
    import json
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print("\n完成。前端重建后打开 http://127.0.0.1:3001/play 应能看到这份演示剧本；当前后端 API 在 http://127.0.0.1:8010。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
