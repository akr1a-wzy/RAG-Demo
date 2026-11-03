import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    from app.retrieval.incremental import IncrementalUpdater

    parser = argparse.ArgumentParser(description="按文件哈希增量更新知识库")
    parser.add_argument("--plan", action="store_true", help="只查看新增、修改、复用计划")
    parser.add_argument("--report", type=Path, help="另存本次计划/更新JSON报告")
    args = parser.parse_args()
    if not args.plan:
        parser.error(
            "此入口仅允许--plan；构建候选请使用rebuild_reviewed_index.py build，激活需独立验收清单"
        )
    report = IncrementalUpdater(ROOT).update(dry_run=args.plan)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
