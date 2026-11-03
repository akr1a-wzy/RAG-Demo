"""从新冻结清单执行独立验收一次；主流程仅接收汇总，不泄露逐题内容。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def holdout_checks(report: dict) -> dict[str, bool]:
    summary = report["summary"]
    return {
        "answerable_30": summary["answerable_count"] == 30,
        "refusal_unscored_5": summary["refusal_not_evaluated"] == 5,
        "evidence_group_recall_085": summary["mean_evidence_group_recall_at_5"] >= 0.85,
        "all_sources_24": summary["all_sources_hit_count"] >= 24,
        "entities_30": summary["entity_correct_count"] == 30,
        "companies_20": len(summary["company_coverage"]) == 20,
        "no_zero_hit_companies": not summary["companies_with_zero_hits"],
        "fields_complete_24": summary["answer_fields_complete_count"] >= 24,
        "top5": report["top_k"] == 5,
    }


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads(args.freeze.read_text(encoding="utf-8"))
    if not all(frozen["selection_gate_checks"].values()):
        raise ValueError("已知回归门槛未通过，独立验收保持封存")
    for relative, expected in frozen["file_hashes"].items():
        if sha(ROOT / relative) != expected:
            raise ValueError(f"冻结后文件发生变化: {relative}")
    manifest = frozen["blind_manifest"]
    dataset = ROOT / manifest["dataset"]
    if sha(dataset) != manifest["sha256"]:
        raise ValueError("封存验收集哈希发生变化")
    # 状态文件以数据集名字为准，换冻结清单也不能重复使用同一封存文件。
    state_path = dataset.with_name(dataset.stem + "_evaluation_state.json")
    report_path = ROOT / "eval/results" / (dataset.stem + "_once.json")
    if report_path.exists():
        raise ValueError("独立验收报告已存在，拒绝覆盖或重复运行")
    state = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "running", "attempt_count": 1,
        "freeze": str(args.freeze.resolve().relative_to(ROOT)),
        "freeze_sha256": sha(args.freeze), "dataset_sha256": manifest["sha256"],
        "report": str(report_path.relative_to(ROOT)), "activated": False,
    }
    descriptor = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
    parameters = frozen["parameters"]
    command = [
        sys.executable, "-X", "utf8", str(ROOT / "scripts/eval_retrieval.py"),
        "--artifact-root", str(ROOT / frozen["candidate_root"]),
        "--dataset", str(dataset), "--output", str(report_path),
        "--mode", parameters["mode"], "--query-expansion", parameters["query_expansion"],
        "--candidate-k", str(parameters["candidate_k"]),
        "--section-boost", str(parameters["section_boost"]),
    ]
    for key, flag in (("page_dedup", "--page-dedup"), ("reranker", "--reranker"),
                      ("task_aware", "--task-aware")):
        if parameters[key]:
            command.append(flag)
    try:
        process = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        log_path = report_path.with_suffix(".log")
        log_path.write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
        state.update({"exit_code": process.returncode,
                      "status": "failed_execution" if process.returncode else "evaluated_once"})
        if process.returncode == 0:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            checks = holdout_checks(report)
            state.update({
                "gate_checks": checks, "gate_passed": all(checks.values()),
                "summary": report["summary"], "report_sha256": sha(report_path),
                "primary_agent_read_case_content_for_tuning": False,
            })
    except Exception as error:
        state.update({"status": "failed_execution", "error": str(error)})
        raise
    finally:
        state["finished_at_utc"] = datetime.now(UTC).isoformat()
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))
    if process.returncode:
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
