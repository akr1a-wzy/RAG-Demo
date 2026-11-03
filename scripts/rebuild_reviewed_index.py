"""基于已审计切片构建、激活或回滚版本化Chroma索引。"""

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    from app.retrieval.reviewed_index import ReviewedIndexUpdater
    from app.retrieval.safe_release import SafeRelease

    parser = argparse.ArgumentParser(description="已审计切片的候选索引工具")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--skip-source-verification", action="store_true")
    activate = commands.add_parser("activate")
    activate.add_argument("--run-dir", type=Path, required=True)
    activate.add_argument("--freeze", type=Path, required=True)
    activate.add_argument("--rehearse-rollback", action="store_true")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = ReviewedIndexUpdater(ROOT).build_candidate(not args.skip_source_verification)
    elif args.command == "activate":
        result = SafeRelease(ROOT).activate(
            args.run_dir, args.freeze, rehearse_rollback=args.rehearse_rollback,
        )
    else:
        result = SafeRelease(ROOT).rollback(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
