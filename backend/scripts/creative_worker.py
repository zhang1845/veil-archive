"""Explicit worker: draft → Compiler/Audit → bounded revision/review → human release."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="执行原创剧本队列；不自动批准发布，不重试模型调用")
    parser.add_argument("--job-id", help="执行指定任务；省略时处理当前排队任务后退出")
    parser.add_argument("--allow-paid", action="store_true", required=True)
    parser.add_argument("--watch", action="store_true", help="持续处理管理端新提交的队列；Ctrl+C 退出")
    args = parser.parse_args()
    from src.core.environment import load_project_environment
    from src.db.session import db_manager
    from src.fusion.authoring_jobs import AuthoringJobStore
    from src.fusion.creative_generation import CreativeStore, run_creative_job
    from src.fusion.provider_config import load_selected_config
    from src.fusion.source_bundles import SourceBundleStore
    try:
        load_project_environment()
        config = load_selected_config("aliyun_bailian")
        store = CreativeStore()
        db_manager.initialize()  # Connect to existing configured schema; never migrate.
        failed = False
        while True:
            identifiers = [args.job_id] if args.job_id else [j["id"] for j in reversed(store.list()) if j["state"] == "QUEUED"]
            for identifier in identifiers:
                result = asyncio.run(run_creative_job(identifier, store, SourceBundleStore(), AuthoringJobStore(db_manager.get_session), config, allow_paid=args.allow_paid))
                print(json.dumps({key: result.get(key) for key in ("id", "state", "error_code", "candidate_version_id", "charged_cost_cny", "current_round", "stop_reason")}, ensure_ascii=False), flush=True)
                failed |= result["state"] != "COMPLETED"
            if not args.watch or args.job_id:
                break
            time.sleep(3)
        return int(failed)
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("原创任务未完成，请查看管理端状态；检查模型配置与现有数据库，未知调用不会自动重试。", file=sys.stderr)
        return 2
    finally:
        db_manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
