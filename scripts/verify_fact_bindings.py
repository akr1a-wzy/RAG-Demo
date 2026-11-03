"""固定事实绑定回归；可选一次真实API验证，复用已冻结来源，不重跑检索盲测。"""

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

from app.qa.config import QASettings
from app.qa.deepseek import DeepSeekClient
from app.qa.evidence import validate_draft
from app.qa.facts import VERSION, source_facts

ROOT = Path(__file__).resolve().parents[1]


def run_case(case):
    source = case["source"]
    payload = {"status": "answer", "claims": [{
        "text": case["text"], "stock_code": source["stock_code"],
        "report_period": source["report_period"], "citations": [
            {"source_id": source["source_id"], "quote": source["content"]},
        ],
    }]}
    resolved = {"query": case["query"], "stock_codes": [source["stock_code"]],
                "report_period": source["report_period"]}
    _, checks = validate_draft(payload, [source], resolved)
    passed = checks["checks_passed"] == case["expected_pass"]
    if case.get("expected_error"):
        passed &= "claim_1_" + case["expected_error"] in checks["errors"]
    return {"case_id": case["case_id"], "kind": case["kind"], "passed": passed,
            "expected_pass": case["expected_pass"], "validation": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model-smoke", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "eval/results/phase8_fact_bindings.json")
    args = parser.parse_args()
    fixture_path = ROOT / "eval/fixtures/phase8_binding_cases.v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    start = perf_counter()
    cases = [run_case(case) for case in fixture["cases"]]
    report = {
        "binding_version": VERSION, "fixture_version": fixture["version"],
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "passed_count": sum(case["passed"] for case in cases), "denominator": len(cases),
        "negative_cases": sum(not case["expected_pass"] for case in cases),
        "positive_cases": sum(case["expected_pass"] for case in cases),
        "recorded_source_cases": sum(case["kind"].startswith("recorded_") for case in cases),
        "cases": cases, "local_validation_seconds": perf_counter() - start,
        "live_model_requests_attempted": int(args.live_model_smoke),
        "retrieval_holdout_rerun": False, "generated_answer_accuracy_accepted": False,
        "scope": (
            "Known-source reconstructed claims and synthetic regression; not blind answer accuracy"
        ),
    }
    if args.live_model_smoke:
        case = next(c for c in fixture["cases"] if c["case_id"] == "recorded_maotai_headcount")
        source = {**case["source"], "facts": source_facts(case["source"])}
        resolved = {"query": case["query"], "stock_codes": [source["stock_code"]],
                    "report_period": source["report_period"]}
        settings = QASettings(_env_file=ROOT / ".env")
        start = perf_counter()
        generated = DeepSeekClient(settings).generate(case["query"], [source], resolved)
        _, validation = validate_draft(generated["draft"], [source], resolved)
        report["live"] = {
            "question": case["query"], "source_provenance": case["provenance"],
            "source": source, "generated": generated, "validation": validation,
            "passed": validation["checks_passed"] and generated["draft"].get("status") == "answer",
            "http_and_validation_seconds": perf_counter() - start,
            "scope": (
                "One paid generation on an already-known retrieved source; no fresh GPU retrieval"
            ),
        }
        secret = settings.deepseek_api_key.get_secret_value()
        if secret and secret in json.dumps(report, ensure_ascii=False):
            raise RuntimeError("拒绝写出包含密钥的诊断报告")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"passed": report["passed_count"], "denominator": len(cases),
                      "live_requests": report["live_model_requests_attempted"],
                      "live_passed": report.get("live", {}).get("passed"),
                      "report": str(args.output)}, ensure_ascii=False))
    if report["passed_count"] != len(cases) or not report.get("live", {}).get("passed", True):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
