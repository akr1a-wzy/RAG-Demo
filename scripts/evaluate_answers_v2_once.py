"""新版问法/组合答案验收：冻结后一次运行，失败或中断也不允许重跑。"""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx
from evaluate_answers_once import aggregate, reserve_attempt

from app.evaluation.answers import normalized, proportion
from app.qa.config import QASettings

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "eval/answer_acceptance_v2_freeze.json"
STATE = ROOT / "eval/answer_acceptance_v2_state.json"
RAW = ROOT / "eval/results/answer_acceptance_v2_raw.jsonl"
REPORT = ROOT / "eval/results/answer_acceptance_v2_once.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_freeze(freeze):
    for relative, expected in freeze["file_hashes"].items():
        if sha(ROOT / relative) != expected:
            raise ValueError(f"验收冻结文件变化: {relative}")


def aggregate_v2(records, cases, gold, contract):
    report = aggregate(records, cases, gold, contract)
    financial = [s for s in report["scores"] if s["group"] == "financial_answerable"]
    report["metrics"]["financial_answer_success"] = proportion(
        sum(s["answer_success"] for s in financial), len(financial))
    facts = {f["fact_id"]: f for f in gold["facts"]}
    by_id = {r["id"]: r["response"] for r in records}
    retrieval, contexts = [], []
    for case in cases:
        if case["group"] == "known_staff_regression" or not case.get("fact_ids"):
            continue
        answer = by_id.get(case["id"], {})
        expected_codes = {facts[k]["stock_code"] for k in case["fact_ids"]}
        resolved = answer.get("resolved", {})
        contexts.append({"id": case["id"], "passed": (
            set(resolved.get("stock_codes", [])) == expected_codes
            and resolved.get("report_period") == "2025")})
        for key in case["fact_ids"]:
            fact = facts[key]
            found = any(s.get("document_id") == fact["document_id"]
                        and s.get("physical_page") == fact["physical_page"]
                        and normalized(fact["value"]) in normalized(s.get("content", ""))
                        for s in answer.get("sources", []))
            retrieval.append({"id": case["id"], "fact_id": key, "found": found})
    report["metrics"]["returned_gold_page_value_coverage"] = proportion(
        sum(r["found"] for r in retrieval), len(retrieval))
    report["metrics"]["resolved_company_period"] = proportion(
        sum(r["passed"] for r in contexts), len(contexts))
    thresholds = contract["additional_gates"]
    report["gates"].update({
        "financial_answer_90": (report["metrics"]["financial_answer_success"]["value"] or 0)
        >= thresholds["financial_answer_success_min"],
        "returned_gold_fact_coverage_90": (
            report["metrics"]["returned_gold_page_value_coverage"]["value"] or 0)
        >= thresholds["retrieved_gold_fact_coverage_min"],
        "context_resolution_100": bool(contexts) and all(r["passed"] for r in contexts),
    })
    attempts = sum(bool(r["response"].get("usage")) or r["response"].get("mode") in {
        "generation_error", "validation_failed"} for r in records)
    report.update({"retrieval_checks": retrieval, "context_checks": contexts,
                   "generation_attempts_observed": attempts,
                   "known_facts_not_fact_holdout": True,
                   "role": "new_question_composition_once_same_facts_same_assistant",
                   "staff_subset_gate_passed": False})
    report["gates"]["generation_attempt_budget_35"] = attempts <= 35
    report["gate_passed"] = len(records) == len(cases) and all(report["gates"].values())
    report["staff_subset_gate_passed"] = all(report["gates"][key] for key in [
        "staff_answer_90", "claim_accuracy_100", "citation_support_100", "refusal_100",
        "controls_100", "unaccepted_or_nonresponsive_claims_zero", "context_resolution_100",
    ]) and len(records) == len(cases)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.parse_args()
    if any(p.exists() for p in [STATE, RAW, REPORT]):
        raise RuntimeError("v2已有状态/输出，禁止重跑或覆盖")
    freeze = read(FREEZE)
    verify_freeze(freeze)
    contract = read(ROOT / "eval/answer_acceptance_v2_contract.json")
    cases = [json.loads(s) for s in (ROOT / "eval/answer_acceptance_v2.jsonl").read_text(
        encoding="utf-8").splitlines() if s.strip()]
    gold = read(ROOT / "eval/answer_acceptance_v2_gold.json")
    settings = QASettings(_env_file=ROOT / ".env")
    secret = settings.deepseek_api_key.get_secret_value()
    if not secret:
        raise RuntimeError("API未配置")
    records, sessions = [], {}
    state = {"version": contract["version"], "status": "running", "attempt_count": 1,
             "planned_case_count": len(cases), "freeze_sha256": sha(FREEZE),
             "started_at_utc": datetime.now(UTC).isoformat(), "in_run_runtime_tuning": False}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        before = health.json()
        if (before["status"] != "ready" or not before["generation_configured"]
                or before["deployment"] != freeze["runtime"]["deployment"]):
            raise RuntimeError("当前服务和冻结部署不匹配")
        reserve_attempt(STATE, state)
        started = perf_counter()
        try:
            with RAW.open("x", encoding="utf-8") as stream:
                for case in cases:
                    start, status = perf_counter(), None
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
                    record = {"id": case["id"], "group": case["group"],
                              "question": case["question"], "http_status": status,
                              "http_seconds": perf_counter() - start, "response": answer,
                              "model_alias_expected": settings.deepseek_model}
                    encoded = json.dumps(record, ensure_ascii=False)
                    if secret in encoded:
                        raise RuntimeError("拒绝写出含密钥的响应")
                    stream.write(encoded + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    records.append(record)
                    print(json.dumps({"completed": len(records), "planned": len(cases),
                                      "id": case["id"], "mode": answer.get("mode")}), flush=True)
            verify_freeze(freeze)
            after = client.get("/api/health")
            after.raise_for_status()
            if after.json()["deployment"] != before["deployment"]:
                raise RuntimeError("验收期间部署改变")
            state["status"] = "evaluated_once"
        except Exception as error:
            state.update(status="interrupted_no_retry", error_type=type(error).__name__)
        report = aggregate_v2(records, cases, gold, contract)
        if state["status"] != "evaluated_once":
            report["gate_passed"] = report["staff_subset_gate_passed"] = False
        report.update(total_runner_seconds=perf_counter() - started, runtime=before,
                      freeze_sha256=sha(FREEZE), raw_sha256=sha(RAW),
                      completed_without_interruption=state["status"] == "evaluated_once")
        with REPORT.open("x", encoding="utf-8") as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
        state.update(completed_case_count=len(records), report=REPORT.relative_to(ROOT).as_posix(),
                     report_sha256=sha(REPORT), raw_sha256=sha(RAW),
                     gate_passed=report["gate_passed"],
                     finished_at_utc=datetime.now(UTC).isoformat())
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: report[k] for k in ["metrics", "gates", "gate_passed",
                                                "generation_attempts_observed"]}))
        if not report["gate_passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
