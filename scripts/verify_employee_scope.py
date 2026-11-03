"""冻结来源的员工小节边界开发回归；不调用检索、生成或一次性验收入口。"""

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

from app.qa.evidence import validate_draft
from app.qa.facts import VERSION, source_facts

ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def semantic_key(fact):
    return fact["metric"], fact["period"], fact["value"], fact["unit"]


def check_claim(source, text, quote, query):
    _, checks = validate_draft({"status": "answer", "claims": [{
        "text": text, "stock_code": source["stock_code"], "report_period": "2025",
        "citations": [{"source_id": source["source_id"], "quote": quote}],
    }]}, [source], {"query": query, "stock_codes": [source["stock_code"]], "report_period": "2025"})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "eval/results/phase10_scope_replay.json")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("报告已存在；重复开发回归须指定新的--output，不能覆盖旧报告")
    started = perf_counter()
    before = read("docs/retrospective/evidence/phase10-employee-scope-before-2026-10-26.json")
    for path, expected in before["frozen_hashes"].items():
        if path != "src/app/qa/facts.py":
            assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    scope = read("eval/fixtures/phase10_employee_scope.v1.json")
    results, targets = [], []
    for case in scope["cases"]:
        source = case["source"]
        facts = source_facts(source)
        totals = [f for f in facts if f["metric"] == "headcount_total" and f["period"] == "2025"]
        passed = bool(totals) == case["expected_current_total"]
        if totals:
            passed &= ({f["value"] for f in totals} == {case["expected_value"]}
                       and {f["unit"] for f in totals} == {"人"})
        results.append({"id": case["id"], "passed": passed,
                        "expected_current_total": case["expected_current_total"],
                        "facts": facts})
        if case["kind"] == "frozen_v1_source_regression":
            target = {"id": case["id"], "fact_count_before": len(source.get("facts", [])),
                      "fact_count_after": len(facts), "totals": totals}
            if len(totals) == 1:
                total = totals[0]
                target["reconstructed_claim_validation"] = check_claim(
                    source, "2025年在职员工数量合计" + case["expected_value"] + "人",
                    total["row_quote"], "2025年在职员工数量合计",
                )
            targets.append(target)
    old_cases = read("eval/fixtures/phase8_binding_cases.v1.json")["cases"]
    old_results = []
    for case in old_cases:
        checks = check_claim(case["source"], case["text"], case["source"]["content"], case["query"])
        passed = checks["checks_passed"] == case["expected_pass"]
        if case.get("expected_error"):
            passed &= "claim_1_" + case["expected_error"] in checks["errors"]
        old_results.append({"id": case["case_id"], "passed": passed})
    records = [json.loads(line) for line in (
        ROOT / "eval/results/answer_acceptance_v1_raw.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
    seen, changes, preserved_claims = set(), [], []
    for record in records:
        response = record["response"]
        for source in response.get("sources", []):
            identity = (source["document_id"], source["physical_page"],
                        source["content"], source.get("section", ""))
            if identity in seen:
                continue
            seen.add(identity)
            previous = {semantic_key(f) for f in source.get("facts", [])}
            current = {semantic_key(f) for f in source_facts(source)}
            if previous != current:
                changes.append({"document_id": source["document_id"],
                                "physical_page": source["physical_page"],
                                "added": sorted(current - previous),
                                "removed": sorted(previous - current)})
        if response.get("mode") == "draft":
            _, checks = validate_draft({"status": "answer", "claims": response["claims"]},
                                       response["sources"], response["resolved"])
            preserved_claims.append({"id": record["id"], "passed": checks["checks_passed"],
                                     "claim_count": len(response["claims"]),
                                     "errors": checks["errors"]})
    report = {
        "version": "phase10-employee-section-development-regression",
        "binding_version": VERSION, "api_calls": 0, "retrieval_calls": 0,
        "scope_cases": results, "scope_passed": sum(r["passed"] for r in results),
        "scope_denominator": len(results), "targets": targets,
        "old_binding_cases": old_results,
        "old_binding_passed": sum(r["passed"] for r in old_results),
        "old_binding_denominator": len(old_results), "unique_frozen_source_contexts": len(seen),
        "source_inventory_changes": changes, "delivered_answer_rechecks": preserved_claims,
        "delivered_answer_cases_passed": sum(r["passed"] for r in preserved_claims),
        "delivered_answer_case_denominator": len(preserved_claims),
        "delivered_claim_count": sum(r["claim_count"] for r in preserved_claims),
        "local_cpu_seconds": perf_counter() - started,
        "gate_passed": all(r["passed"] for r in results + old_results + preserved_claims)
                       and not any(c["removed"] for c in changes),
        "original_answer_v1_report_unchanged": True,
        "new_answer_acceptance_performed": False, "generated_answer_accuracy_accepted": False,
        "limits": "Bindings on frozen sources/reconstructed claims only; no new answers. "
                  "Rejected model drafts were not present and cannot be revalidated.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: report[k] for k in [
        "scope_passed", "scope_denominator", "old_binding_passed", "old_binding_denominator",
        "unique_frozen_source_contexts", "delivered_answer_cases_passed",
        "delivered_answer_case_denominator", "delivered_claim_count", "gate_passed",
    ]}, ensure_ascii=False))
    if not report["gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
