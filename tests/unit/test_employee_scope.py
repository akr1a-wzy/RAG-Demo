import json
from pathlib import Path

import pytest

from app.qa.evidence import validate_draft
from app.qa.facts import source_facts

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "eval/fixtures/phase10_employee_scope.v1.json").read_text(
    encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_employee_section_year_boundary(case):
    source = case["source"]
    totals = [f for f in source_facts(source)
              if f["metric"] == "headcount_total" and f["period"] == "2025"]
    assert bool(totals) == case["expected_current_total"], totals
    if totals:
        assert {f["value"] for f in totals} == {case["expected_value"]}
        assert {f["unit"] for f in totals} == {"人"}


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "frozen_v1_source_regression"],
                         ids=lambda c: c["id"])
def test_reconstructed_target_claim_binds_without_inventing_quote(case):
    source = case["source"]
    total = next(f for f in source_facts(source) if f["metric"] == "headcount_total")
    assert total["year_context_origin"] == "body_employee_heading"
    assert total["year_context_quote"] in source["content"]
    _, checks = validate_draft({"status": "answer", "claims": [{
        "text": "2025年在职员工数量合计" + case["expected_value"] + "人",
        "stock_code": source["stock_code"], "report_period": "2025",
        "citations": [{"source_id": source["source_id"], "quote": total["row_quote"]}],
    }]}, [source], {"query": "2025年在职员工数量合计", "stock_codes": [source["stock_code"]],
                   "report_period": "2025"})
    assert checks["checks_passed"] and checks["fact_bindings_verified"]
    assert not checks["generated_answer_accuracy_accepted"]
