"""已见v2的开发回归；复用原评分函数，绝不改写原验收题、报告或一次状态。"""

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx
from evaluate_answers_v2_once import aggregate_v2

from app.qa.config import QASettings

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(freeze):
    for path, expected in freeze["file_hashes"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"开发冻结文件变化: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,60}", args.run_id):
        raise ValueError("run-id须为安全目录名")
    freeze = read(args.freeze)
    verify(freeze)
    roles = read(ROOT / "eval/dataset_roles.json")
    if roles["answer_acceptance_v2"]["role"] != "fixed_regression_after_failed_acceptance":
        raise ValueError("须先将v2明确登记为已见回归")
    cases = [json.loads(s) for s in (ROOT / "eval/answer_acceptance_v2.jsonl").read_text(
        encoding="utf-8").splitlines() if s.strip()]
    gold = read(ROOT / "eval/answer_acceptance_v2_gold.json")
    contract = read(ROOT / "eval/answer_acceptance_v2_contract.json")
    secret = QASettings(_env_file=ROOT / ".env").deepseek_api_key.get_secret_value()
    if not secret:
        raise RuntimeError("API未配置")
    out = ROOT / "eval/results/development" / args.run_id
    out.mkdir(parents=True, exist_ok=False)
    sessions, records = {}, []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        runtime = health.json()
        assert runtime["status"] == "ready"
        assert runtime["deployment"]["qa_evidence_policy"] == freeze["qa_evidence_policy"]
        started = perf_counter()
        with (out / "raw.jsonl").open("x", encoding="utf-8") as stream:
            for case in cases:
                status, start = None, perf_counter()
                try:
                    if case.get("depends_on"):
                        sid = sessions[case["depends_on"]]
                    else:
                        created = client.post("/api/sessions")
                        created.raise_for_status()
                        sid = created.json()["session_id"]
                    sessions[case["id"]] = sid
                    response = client.post("/api/chat", json={
                        "session_id": sid, "question": case["question"],
                        "evidence_only": case.get("evidence_only", False),
                    })
                    status = response.status_code
                    answer = response.json() if status == 200 else {"mode": "http_error"}
                except (httpx.HTTPError, ValueError, KeyError) as error:
                    answer = {"mode": "transport_error", "error_type": type(error).__name__}
                record = {"id": case["id"], "question": case["question"], "group": case["group"],
                          "response": answer, "http_status": status,
                          "http_seconds": perf_counter() - start}
                encoded = json.dumps(record, ensure_ascii=False)
                if secret in encoded:
                    raise RuntimeError("拒绝写出含密钥的响应")
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                records.append(record)
                print(json.dumps({"done": len(records), "id": case["id"],
                                  "mode": answer.get("mode")}), flush=True)
        elapsed = perf_counter() - started
        assert client.get("/api/health").json()["deployment"] == runtime["deployment"]
    verify(freeze)
    report = aggregate_v2(records, cases, gold, contract)
    report.update(
        date="2026-10-26", recorded_at_utc=datetime.now(UTC).isoformat(), run_id=args.run_id,
        role="seen_v2_development_regression_not_new_acceptance",
        new_answer_acceptance_performed=False, generated_answer_accuracy_accepted=False,
        runtime=runtime, total_runner_seconds=elapsed, in_run_retries=0,
        api_responses_with_usage=sum(bool(r["response"].get("usage")) for r in records),
        usage={k: sum(r["response"].get("usage", {}).get(k, 0) for r in records)
               for k in ["prompt_tokens", "completion_tokens", "total_tokens"]},
        qa_retrieval_calls_recorded=sum(len(r["response"].get(
            "fact_selection", {}).get("retrieval_queries", [])) for r in records),
        timing_scope=("Initialized local HTTP/GPU, remote generation, validation and sessions; "
                      "no startup"),
        freeze_sha256=hashlib.sha256(args.freeze.read_bytes()).hexdigest(),
    )
    with (out / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ["metrics", "gate_passed", "usage",
                                             "generation_attempts_observed"]}))


if __name__ == "__main__":
    main()
