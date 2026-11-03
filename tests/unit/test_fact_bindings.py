import json
from pathlib import Path

import pytest

from app.qa.evidence import validate_draft
from app.qa.facts import source_facts

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "eval/fixtures/phase8_binding_cases.v1.json").read_text(
    encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_row_year_unit_contract(case):
    s = case["source"]
    payload = {"status": "answer", "claims": [{
        "text": case["text"], "stock_code": s["stock_code"], "report_period": s["report_period"],
        "citations": [{"source_id": s["source_id"], "quote": s["content"]}],
    }]}
    _, checks = validate_draft(payload, [s], {
        "query": case["query"], "stock_codes": [s["stock_code"]],
        "report_period": s["report_period"],
    })
    assert checks["checks_passed"] == case["expected_pass"], checks
    if case.get("expected_error"):
        assert "claim_1_" + case["expected_error"] in checks["errors"]
    assert not checks["generated_answer_accuracy_accepted"]
    assert not checks["financial_row_column_semantics_verified"]


def test_catalog_cannot_be_forged_by_precomputed_source_facts():
    s = {**CASES[1]["source"], "facts": [{"metric": "revenue", "period": "2025", "value": "100"}]}
    assert source_facts(s)[0]["value"] == "200"


def test_bound_fact_retains_header_row_unit_provenance():
    fact = source_facts(CASES[0]["source"])[0]
    assert fact["period"] == "2025" and fact["value"] == "200"
    assert fact["header_quote"] == "项目 2025年 2024年"
    assert fact["column_index"] == 1 and fact["unit_basis"] == "local_table_unit"
    assert fact["unit_quote"] == "单位：百万元"


def test_verified_column_binding_flag_is_limited_to_explicit_columns():
    case = CASES[0]
    s = case["source"]
    _, checks = validate_draft({"status": "answer", "claims": [{
        "text": case["text"], "stock_code": s["stock_code"], "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": s["content"]}],
    }]}, [s], {"query": case["query"], "stock_codes": [s["stock_code"]], "report_period": "2025"})
    assert checks["explicit_year_column_bindings_verified"]
    assert not checks["financial_row_column_semantics_verified"]


def test_implicit_unit_is_labelled_and_original_quote_is_not_rewritten():
    s = next(c["source"] for c in CASES if c["case_id"] == "headcount_implicit_unit")
    fact = source_facts(s)[0]
    assert fact["unit"] == "人" and fact["unit_basis"] == "employee_count_semantics"
    assert "人" not in fact["row_quote"]


@pytest.mark.parametrize("content,section", [
    ("2024年员工情况\n在职员工的数量合计 100", "员工情况"),
    ("在职员工的数量合计 100", "2024年员工情况"),
    ("报告期末员工情况\n在职员工的数量合计 100", "2024年员工情况"),
    ("2024年员工情况\n报告期末员工情况\n在职员工的数量合计 100", "员工情况"),
    ("在职员工的数量合计 100.5", "员工情况"),
])
def test_headcount_does_not_infer_current_year_or_fractional_people(content, section):
    s = next(c["source"] for c in CASES if c["case_id"] == "headcount_implicit_unit")
    assert not source_facts({**s, "content": content, "section": section})
