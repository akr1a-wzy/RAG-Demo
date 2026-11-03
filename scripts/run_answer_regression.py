"""已见答案集的真实HTTP开发回归；独立运行目录，不写旧一次验收状态。"""

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx

from app.evaluation.answers import score_case, summarize
from app.qa.config import QASettings

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,60}", args.run_id):
        raise ValueError("run-id须为小写字母开头的安全目录名")
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    for path, expected in freeze["file_hashes"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    dataset = ROOT / "eval/answer_acceptance_v1.jsonl"
    cases = [json.loads(s) for s in dataset.read_text(encoding="utf-8").splitlines() if s.strip()]
    roles = json.loads((ROOT / "eval/dataset_roles.json").read_text(encoding="utf-8"))
    assert roles["answer_acceptance_v1"]["role"] == "fixed_regression_after_failed_acceptance"
    gold = json.loads((ROOT / "eval/answer_acceptance_v1_gold.json").read_text(encoding="utf-8"))
    facts = {f["fact_id"]: f for f in gold["facts"]}
    contract_path = ROOT / "eval/answer_acceptance_v1_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    out = ROOT / "eval/results/development" / args.run_id
    out.mkdir(parents=True, exist_ok=False)
    sessions, records, scores = {}, [], []
    settings = QASettings(_env_file=ROOT / ".env")
    secret = settings.deepseek_api_key.get_secret_value()
    if not secret:
        raise RuntimeError("API未配置")
    started = perf_counter()
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        runtime = health.json()
        assert runtime["deployment"].get("qa_evidence_policy") == freeze["qa_evidence_policy"]
        if freeze.get("binding_version"):
            assert runtime["deployment"].get("binding_version") == freeze["binding_version"]
        with (out / "raw.jsonl").open("x", encoding="utf-8") as stream:
            for case in cases:
                sid = sessions.get(case.get("depends_on"))
                if not sid:
                    created = client.post("/api/sessions")
                    created.raise_for_status()
                    sid = created.json()["session_id"]
                sessions[case["id"]] = sid
                start = perf_counter()
                status = None
                try:
                    response = client.post("/api/chat", json={
                        "session_id": sid, "question": case["question"],
                        "evidence_only": case.get("evidence_only", False),
                    })
                    status = response.status_code
                    data = response.json() if status == 200 else {"mode": "http_error"}
                except (httpx.HTTPError, ValueError) as error:
                    data = {"mode": "transport_error", "error_type": type(error).__name__}
                record = {"id": case["id"], "question": case["question"], "group": case["group"],
                          "response": data, "http_status": status,
                          "seconds": perf_counter() - start}
                encoded = json.dumps(record, ensure_ascii=False)
                assert not secret or secret not in encoded
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                records.append(record)
                scores.append(score_case(case, data, facts))
                print(json.dumps({"done": len(records), "id": case["id"],
                                  "mode": data.get("mode")}),
                      flush=True)
    for path, expected in freeze["file_hashes"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    report = summarize(scores, contract)
    report.update({"date": "2026-10-26", "run_id": args.run_id,
                   "role": "development_regression_on_seen_v1_not_new_acceptance",
                   "recorded_at_utc": datetime.now(UTC).isoformat(), "scores": scores,
                   "case_count": len(cases), "api_responses_with_usage": sum(
                       bool(r["response"].get("usage")) for r in records),
                   "usage": {k: sum(r["response"].get("usage", {}).get(k, 0) for r in records)
                             for k in ["prompt_tokens", "completion_tokens", "total_tokens"]},
                   "total_seconds": perf_counter() - started, "runtime": runtime,
                   "freeze_sha256": hashlib.sha256(args.freeze.read_bytes()).hexdigest(),
                   "new_answer_acceptance_performed": False})
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["metrics", "gate_passed", "staff_subset_gate_passed",
                                             "api_responses_with_usage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
