"""核对已冻结配置，封存盲测只运行一次；向终端仅输出汇总。"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    freeze_path = ROOT / "eval/results/phase3_selection_freeze.json"
    frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
    if not (frozen["old_gate_passed"] and frozen["regression_gate_passed"]):
        raise ValueError("旧集和固定回归门槛未通过，盲测保持封存")
    for relative, expected in frozen["file_hashes"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"冻结后文件发生变化: {relative}")
    manifest = frozen["blind_manifest"]
    dataset = ROOT / manifest["dataset"]
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("封存盲测文件哈希发生变化")
    report_path = ROOT / "eval/results/blind_v3_once.json"
    state_path = ROOT / "eval/sealed/blind_v3_evaluation_state.json"
    if report_path.exists():
        raise ValueError("盲测报告已存在，拒绝重复运行")
    state = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "running", "attempt_count": 1,
        "freeze_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
        "dataset_sha256": manifest["sha256"],
        "report": str(report_path.relative_to(ROOT)),
    }
    # 排他创建标记，同一盲测不允许并发、覆盖或查看结果后重复调参。
    descriptor = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
    parameters = frozen["parameters"]
    command = [
        sys.executable, "-X", "utf8", str(ROOT / "scripts/eval_retrieval.py"),
        "--artifact-root", str(ROOT / frozen["candidate_root"]),
        "--dataset", str(dataset), "--output", str(report_path),
        "--mode", parameters["mode"],
        "--query-expansion", parameters["query_expansion"],
        "--candidate-k", str(parameters["candidate_k"]),
        "--section-boost", str(parameters["section_boost"]),
    ]
    for key, flag in (("page_dedup", "--page-dedup"), ("reranker", "--reranker"),
                      ("task_aware", "--task-aware")):
        if parameters[key]:
            command.append(flag)
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    log_path = ROOT / "eval/results/blind_v3_once_process.log"
    log_path.write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
    state["finished_at_utc"] = datetime.now(UTC).isoformat()
    state["exit_code"] = process.returncode
    state["status"] = "failed_execution" if process.returncode else "evaluated_once"
    if process.returncode == 0:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        summary = report["summary"]
        checks = {
            "answerable_30": summary["answerable_count"] == 30,
            "evidence_group_recall_085": summary["mean_evidence_group_recall_at_5"] >= 0.85,
            "all_sources_24": summary["all_sources_hit_count"] >= 24,
            "entities_30": summary["entity_correct_count"] == 30,
            "companies_20": len(summary["company_coverage"]) == 20,
            "no_zero_hit_companies": not summary["companies_with_zero_hits"],
            "fields_complete_24": summary["answer_fields_complete_count"] >= 24,
            "top5": report["top_k"] == 5,
        }
        state.update({
            "gate_checks": checks, "gate_passed": all(checks.values()),
            "summary": summary,
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "primary_agent_may_read_case_content_for_tuning": False,
            "activated": False,
        })
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if process.returncode:
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
