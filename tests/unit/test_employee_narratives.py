import json
from pathlib import Path

import pytest

from app.qa.evidence import validate_draft
from app.qa.facts import source_facts
from app.qa.selection import select_target_facts

ROOT = Path(__file__).resolve().parents[2]
REAL = json.loads((ROOT / "docs/retrospective/evidence/"
                   "phase11-employee-coverage-before-2026-10-26.json").read_text(
                       encoding="utf-8"))["narrative_cases"]
VALUES = {"600036": "121,585", "600028": "351,104", "601318": "258,806"}


def synthetic(content, **extra):
    return {"source_id": "S1", "document_id": "synthetic", "stock_code": "000001",
            "stock_name": "公司甲", "report_period": "2025", "document_type": "annual_report",
            "section": "员工情况", "layout_review_required": False, "content": content, **extra}


@pytest.mark.parametrize("case", REAL, ids=lambda c: c["id"])
def test_real_narratives_keep_company_period_unit_and_original_quote(case):
    source = case["source"]
    facts = source_facts(source)
    assert len(facts) == 1
    fact = facts[0]
    assert fact["value"] == VALUES[source["stock_code"]]
    assert fact["period"] == "2025" and fact["unit"] == "人"
    assert fact["row_quote"] in source["content"]
    text = "2025年在职员工总数为" + fact["value"] + "人"
    if fact["entity_scope"] == "system":
        text = "中国平安体系内" + text
    _, checks = validate_draft({"status": "answer", "claims": [{
        "text": text, "stock_code": source["stock_code"], "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": fact["row_quote"]}],
    }]}, [source], {"query": case["question"], "stock_codes": [source["stock_code"]],
                   "report_period": "2025"})
    assert checks["checks_passed"]


@pytest.mark.parametrize("content,extra", [
    ("本集团共有员工100人。", {}),
    ("截至报告期末，本集团共有员工100人。", {"section": "2024年员工情况"}),
    ("2024年员工情况\n截至报告期末，本集团共有员工100人。", {}),
    ("截至2025年末，本集团共有员工100.5人。", {}),
    ("截至2025年末，本集团共有员工-100人。", {}),
    ("截至2025年末，本集团共有员工100人。", {"layout_review_required": True}),
    ("截至2025年末，本公司拥有员工100名。", {"section": "某子公司员工情况"}),
    ("预计截至2025年末，本集团共有员工100人。", {}),
    ("截至2025年末，公司乙体系内共有在职员工100人。", {}),
])
def test_narrative_ambiguity_is_not_accepted_as_current_employee_fact(content, extra):
    assert not source_facts(synthetic(content, **extra))


def test_explicit_historical_narrative_is_not_relabelled_as_report_year():
    facts = source_facts(synthetic("截至2024年12月31日，本集团共有员工100人。"))
    assert len(facts) == 1 and facts[0]["period"] == "2024"


def test_name_unit_is_person_count_not_a_magnitude_conversion():
    fact = source_facts(synthetic("于2025年12月31日，本公司拥有员工100名。"))[0]
    assert fact["unit"] == "人" and fact["raw_unit"] == "名"


def test_narrative_claim_wording_can_bind_without_rewriting_it_as_table_label():
    source = REAL[0]["source"]
    fact = source_facts(source)[0]
    _, checks = validate_draft({"status": "answer", "claims": [{
        "text": "招商银行2025年本集团共有员工121,585人（含派遣人员）。",
        "stock_code": "600036", "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": fact["row_quote"]}],
    }]}, [source], {"query": REAL[0]["question"], "stock_codes": ["600036"],
                   "report_period": "2025"})
    assert checks["checks_passed"]


def test_dispatched_worker_qualifier_cannot_be_removed_from_citation():
    source = REAL[0]["source"]
    _, checks = validate_draft({"status": "answer", "claims": [{
        "text": "2025年员工总数121,585人", "stock_code": "600036", "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": "本集团共有员工121,585人"}],
    }]}, [source], {"query": "员工数量含派遣", "stock_codes": ["600036"], "report_period": "2025"})
    assert not checks["checks_passed"]


def total_source(code="000001", value="100", metric="headcount_total"):
    return {"source_id": "S1", "facts": [{"stock_code": code, "period": "2025", "metric": metric,
                                         "value": value, "unit": "人"}]}


def resolved(query="员工人数", codes=None):
    return {"query": query, "stock_codes": codes or ["000001"], "report_period": "2025"}


def test_target_selection_does_not_include_parent_and_subsidiary_metrics():
    source = total_source()
    source["facts"] += total_source(metric="headcount_parent")["facts"]
    selected, state = select_target_facts([source], resolved())
    assert state["status"] == "ready" and len(selected[0]["facts"]) == 1
    assert len(source["facts"]) == 2


def test_comparison_requires_every_company_fact_before_generation():
    _, state = select_target_facts([total_source()], resolved(codes=["000001", "000002"]))
    assert state["status"] == "missing" and state["missing_companies"] == ["000002"]


def test_conflicting_counts_do_not_silently_pick_one():
    _, state = select_target_facts([total_source(), total_source(value="200")], resolved())
    assert state["status"] == "ambiguous"


def test_system_population_requires_explicit_requested_scope():
    source = {**REAL[2]["source"], "facts": source_facts(REAL[2]["source"])}
    _, state = select_target_facts([source], resolved("中国平安员工数量", ["601318"]))
    assert state["status"] == "missing"
    _, state = select_target_facts([source], resolved("中国平安体系内在职员工数量", ["601318"]))
    assert state["status"] == "ready"


@pytest.mark.parametrize("query", [
    "在职员工数量中养猫的有多少人", "员工人数里身高超过190厘米有多少",
])
def test_unsupported_subset_is_not_answered_with_total_count(query):
    _, state = select_target_facts([total_source()], resolved(query))
    assert state["status"] == "unsupported_employee_subset"
