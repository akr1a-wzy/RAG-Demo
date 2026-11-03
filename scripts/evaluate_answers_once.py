"""冻结版本的真实HTTP答案验收，只允许一次请求批次；--score-only离线复算不调用API。"""

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx

from app.evaluation.answers import score_case, summarize
from app.qa.config import QASettings

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "eval/answer_acceptance_v1_freeze.json"
STATE = ROOT / "eval/answer_acceptance_v1_state.json"
RAW = ROOT / "eval/results/answer_acceptance_v1_raw.jsonl"
REPORT = ROOT / "eval/results/answer_acceptance_v1_once.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reserve_attempt(path, state):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def verify_freeze(freeze):
    for relative, digest in freeze["file_hashes"].items():
        if sha(ROOT / relative) != digest:
            raise ValueError(f"冻结文件变化: {relative}")


def aggregate(records, cases, gold, contract):
    by_id = {r["id"]: r for r in records}
    facts = {f["fact_id"]: f for f in gold["facts"]}
    scores = [score_case(c, by_id.get(c["id"], {}).get("response", {"mode": "not_run"}), facts)
              for c in cases]
    report = summarize(scores, contract)
    durations = sorted(r["http_seconds"] for r in records)
    report.update({
        "version": contract["version"], "attempt_count": 1, "planned_case_count": len(cases),
        "completed_case_count": len(records), "scores": scores,
        "modes": dict(Counter(r["response"].get("mode") for r in records)),
        "generation_responses_with_usage": sum(bool(r["response"].get("usage")) for r in records),
        "generation_errors": sum(r["response"].get("mode") == "generation_error" for r in records),
        "usage": {key: sum(r["response"].get("usage", {}).get(key, 0) for r in records)
                  for key in ["prompt_tokens", "completion_tokens", "total_tokens"]},
        "http_seconds_sum": sum(durations), "http_seconds_max": max(durations, default=None),
        "timing_scope": contract["timing"], "novelty_and_limits": contract["novelty"],
        "gold_independence": contract["gold_standard"], "authoring_scope": contract["scope"],
        "model_alias": records[0].get("model_alias_expected") if records else None,
        "provider_weight_revision_pinned": False, "runtime_application_changed": False,
        "blind_retrieval_v4_rerun": False,
    })
    if len(records) != len(cases):
        report["gate_passed"] = report["staff_subset_gate_passed"] = False
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--score-only", action="store_true")
    args = parser.parse_args()
    freeze = read(FREEZE)
    verify_freeze(freeze)
    contract = read(ROOT / "eval/answer_acceptance_v1_contract.json")
    cases = [json.loads(s) for s in (ROOT / "eval/answer_acceptance_v1.jsonl").read_text(
        encoding="utf-8").splitlines() if s.strip()]
    gold = read(ROOT / "eval/answer_acceptance_v1_gold.json")
    if args.score_only:
        records = [json.loads(s) for s in RAW.read_text(encoding="utf-8").splitlines() if s.strip()]
        report = aggregate(records, cases, gold, contract)
        print(json.dumps({k: v for k, v in report.items() if k != "scores"}, ensure_ascii=False))
        return
    if RAW.exists() or REPORT.exists():
        raise RuntimeError("原始输出或报告已存在，禁止覆盖/重跑")
    settings = QASettings(_env_file=ROOT / ".env")
    secret = settings.deepseek_api_key.get_secret_value()
    if not secret:
        raise RuntimeError("本机未配置生成API")
    records, sessions = [], {}
    state = {"version": contract["version"], "status": "running", "attempt_count": 1,
             "freeze_sha256": sha(FREEZE), "started_at_utc": datetime.now(UTC).isoformat(),
             "planned_case_count": len(cases), "primary_agent_tuned_after_results": False}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        before = health.json()
        if not (before["generation_configured"] and before["status"] == "ready"
                and before["deployment"]["release_id"] == freeze["release_id"]):
            raise RuntimeError("服务未就绪或发布版本不匹配")
        reserve_attempt(STATE, state)
        started = perf_counter()
        try:
            with RAW.open("x", encoding="utf-8") as stream:
                for case in cases:
                    start = perf_counter()
                    status = None
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
                        result = response.json() if status == 200 else {"mode": "http_error"}
                    except (httpx.HTTPError, ValueError, KeyError) as error:
                        result = {"mode": "transport_error", "error_type": type(error).__name__}
                    record = {"id": case["id"], "group": case["group"],
                              "question": case["question"], "http_status": status,
                              "http_seconds": perf_counter() - start, "response": result,
                              "model_alias_expected": settings.deepseek_model}
                    encoded = json.dumps(record, ensure_ascii=False)
                    if secret in encoded:
                        raise RuntimeError("拒绝保存含密钥的响应")
                    stream.write(encoded + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    records.append(record)
                    print(json.dumps({"completed": len(records), "planned": len(cases),
                                      "id": case["id"], "mode": result.get("mode")}), flush=True)
            verify_freeze(freeze)
            end_health = client.get("/api/health")
            end_health.raise_for_status()
            if end_health.json()["deployment"] != before["deployment"]:
                raise RuntimeError("评测期间活动发布改变")
            state["status"] = "evaluated_once"
        except Exception as error:
            state.update(status="interrupted_no_retry", error_type=type(error).__name__)
        report = aggregate(records, cases, gold, contract)
        report.update(total_runner_seconds=perf_counter() - started,
                      runtime=before, freeze_sha256=sha(FREEZE), raw_sha256=sha(RAW))
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state.update(completed_case_count=len(records), report=REPORT.relative_to(ROOT).as_posix(),
                     report_sha256=sha(REPORT), raw_sha256=sha(RAW),
                     gate_passed=report["gate_passed"],
                     finished_at_utc=datetime.now(UTC).isoformat())
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "scores"}, ensure_ascii=False))
        if not report["gate_passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
