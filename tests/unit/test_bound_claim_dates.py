import json
from pathlib import Path

import pytest

from app.qa.evidence import validate_draft
from app.qa.reviews import ReviewedEmployeeFacts

ROOT = Path(__file__).resolve().parents[2]


def check(text, content="截至2025年12月31日，本集团共有员工100人。"):
    source = {"source_id": "S1", "document_id": "synthetic", "stock_code": "000001",
              "report_period": "2025", "stock_name": "公司甲", "document_type": "annual_report",
              "section": "员工情况", "layout_review_required": False, "content": content}
    return validate_draft({"status": "answer", "claims": [{
        "text": text, "stock_code": "000001", "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": content}],
    }]}, [source], {"query": "2025年员工人数", "stock_codes": ["000001"],
                   "report_period": "2025"})[1]


@pytest.mark.parametrize("date", ["2025年12月31日", "2025-12-31", "2025/12/31"])
def test_bound_calendar_date_is_not_a_second_numeric_fact(date):
    assert check(date + "本集团共有员工100人")["checks_passed"]


@pytest.mark.parametrize("date", ["2025年12月30日", "2025年11月31日", "2024年12月31日"])
def test_wrong_asof_date_cannot_borrow_other_numbers(date):
    content = "截至2025年12月31日，本集团共有员工100人。另有2025年12月30日会议。"
    assert not check(date + "本集团共有员工100人", content)["checks_passed"]


def test_page_date_without_row_context_is_not_a_bound_employee_date():
    content = "2025年12月31日董事会\n八、公司员工情况\n在职员工的数量合计 100"
    assert not check("2025年12月31日在职员工数量合计100人", content)["checks_passed"]


def test_date_numbers_cannot_be_reused_as_extra_headcount():
    assert not check("2025年12月31日本集团共有员工100人，另有12人")["checks_passed"]


def test_joint_parent_major_subsidiary_total_is_not_parent_only():
    content = "报告期末在职员工的数量合计（人）100"
    assert check("2025年母公司及主要子公司在职员工的数量合计100人", content)["checks_passed"]
    assert not check("2025年母公司在职员工总数100人", content)["checks_passed"]


def test_reproduced_provider_drafts_pass_without_loosening_date_or_unit_checks():
    records = [json.loads(s) for s in (ROOT / "eval/results/development/employee-fixes-v1/raw.jsonl"
                                      ).read_text(encoding="utf-8").splitlines()]
    by_id = {r["id"]: r for r in records}
    diagnostics = json.loads((ROOT / "eval/results/development/employee-fixes-v1/"
                             "provider_diagnostics.json").read_text(encoding="utf-8"))
    for case in diagnostics["cases"]:
        response = by_id[case["id"]]["response"]
        _, checks = validate_draft(case["generated"]["draft"], response["sources"],
                                   response["resolved"], ReviewedEmployeeFacts(ROOT))
        assert checks["checks_passed"], checks


def test_system_total_cannot_be_relabelled_as_parent_and_subsidiary_total():
    path = ROOT / "docs/retrospective/evidence/phase13-entity-scope-before-2026-10-26.json"
    frozen = json.loads(path.read_text(encoding="utf-8"))
    source = {"source_id": "S1", "stock_code": "601318", "report_period": "2025",
              "stock_name": "中国平安", "document_type": "annual_report", "section": "员工情况",
              "content": frozen["source_fact"]["row_quote"], "layout_review_required": False}
    _, checks = validate_draft(frozen["payload"], [source], {
        "query": "中国平安2025年体系内在职员工数量", "stock_codes": ["601318"],
        "report_period": "2025",
    })
    assert not checks["checks_passed"]
    assert "claim_1_entity_scope_mismatch" in checks["errors"]
