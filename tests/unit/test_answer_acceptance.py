import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.evaluation.answers import score_case, summarize


def fixture():
    fact = {"fact_id": "A:2025:headcount_total", "stock_code": "000001", "stock_name": "公司甲",
            "period": "2025", "metric": "headcount_total", "value": "100", "unit": "人",
            "document_id": "A", "physical_page": 10, "row_label": "在职员工的数量合计"}
    case = {"id": "a", "group": "new_staff_single", "expected_mode": "draft",
            "fact_ids": [fact["fact_id"]]}
    response = {"mode": "draft", "claims": [{
        "text": "公司甲2025年在职员工数量合计为100人。", "stock_code": "000001",
        "report_period": "2025", "citations": [
            {"source_id": "S1", "quote": "在职员工的数量合计 100"}],
    }], "sources": [{"source_id": "S1", "document_id": "A", "physical_page": 10,
                      "content": "在职员工的数量合计 100\n资产总计 100"}]}
    return case, response, {fact["fact_id"]: fact}


def test_pdf_gold_driven_answer_and_unit_semantics():
    case, response, facts = fixture()
    scored = score_case(case, response, facts)
    assert scored["answer_success"] and scored["matched_fact_count"] == 1


@pytest.mark.parametrize("change", [
    "company", "period", "number", "unit", "scope", "extra_assertion", "average", "extra_number",
])
def test_incorrect_delivered_claims_fail_independent_gold(change):
    case, response, facts = fixture()
    claim = response["claims"][0]
    if change == "company":
        claim["stock_code"] = "000002"
    elif change == "period":
        claim["report_period"] = "2024"
    elif change == "number":
        claim["text"] = claim["text"].replace("100人", "200人")
    elif change == "unit":
        claim["text"] = claim["text"].replace("100人", "100万人")
    elif change == "scope":
        claim["text"] = "公司甲2025年母公司在职员工数量为100人"
    elif change == "extra_assertion":
        claim["text"] += "，同比增长并排名领先。"
    elif change == "average":
        claim["text"] = "公司甲2025年平均员工数量100人"
    else:
        claim["text"] += "，分布在9个地方。"
    scored = score_case(case, response, facts)
    assert not scored["answer_success"] and not scored["claims"][0]["correct"]


@pytest.mark.parametrize("change", [
    "missing", "unknown", "invented_quote", "wrong_row", "wrong_page",
])
def test_quote_presence_is_not_sufficient_for_citation_acceptance(change):
    case, response, facts = fixture()
    claim = response["claims"][0]
    if change == "missing":
        claim["citations"] = []
    elif change == "unknown":
        claim["citations"][0]["source_id"] = "S9"
    elif change == "invented_quote":
        claim["citations"][0]["quote"] = "不存在的员工 100"
    elif change == "wrong_row":
        claim["citations"][0]["quote"] = "资产总计 100"
    else:
        response["sources"][0]["physical_page"] = 11
    scored = score_case(case, response, facts)
    assert not scored["answer_success"] and scored["claims"][0]["correct"]
    assert not scored["claims"][0]["all_citations_supported"]


def test_comparison_requires_both_companies_and_not_duplicate_claims():
    case, response, facts = fixture()
    other = copy.deepcopy(next(iter(facts.values())))
    other.update(fact_id="B:2025:headcount_total", stock_code="000002", stock_name="公司乙")
    facts[other["fact_id"]] = other
    case["group"] = "staff_comparison"
    case["fact_ids"].append(other["fact_id"])
    response["claims"].append(copy.deepcopy(response["claims"][0]))
    scored = score_case(case, response, facts)
    assert not scored["complete"] and scored["matched_fact_count"] == 1


def test_evidence_only_never_counts_as_correct_answer():
    case, response, facts = fixture()
    response.update(mode="evidence_only", claims=[])
    scored = score_case(case, response, facts)
    assert not scored["answer_success"] and scored["safe_abstention"]
    assert scored["matched_fact_count"] == 0


def test_true_but_unrequested_fact_cannot_replace_refusal():
    case, response, facts = fixture()
    case.update(group="no_evidence_refusal", expected_mode="refused", fact_ids=[])
    scored = score_case(case, response, facts)
    assert not scored["mode_contract_passed"] and not scored["claims"][0]["correct"]


def test_no_delivered_answers_cannot_vacuously_pass_precision_gate():
    case, response, facts = fixture()
    response.update(mode="evidence_only", claims=[])
    contract = json.loads((Path(__file__).resolve().parents[2] /
                           "eval/answer_acceptance_v1_contract.json").read_text(encoding="utf-8"))
    summary = summarize([score_case(case, response, facts)], contract)
    assert summary["metrics"]["delivered_claim_accuracy"]["value"] is None
    assert not summary["gate_passed"] and not summary["gates"]["claim_accuracy_100"]


def test_employee_scope_qualifier_must_be_supported():
    case, response, facts = fixture()
    next(iter(facts.values()))["required_context"] = ["派遣"]
    assert not score_case(case, response, facts)["answer_success"]


def test_once_state_cannot_be_reset_or_overwritten(tmp_path):
    runner = Path(__file__).resolve().parents[2] / "scripts/evaluate_answers_once.py"
    spec = importlib.util.spec_from_file_location("answer_once_test_runner", runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "state.json"
    module.reserve_attempt(state, {"attempt_count": 1, "status": "running"})
    before = state.read_bytes()
    with pytest.raises(FileExistsError):
        module.reserve_attempt(state, {"attempt_count": 0})
    assert state.read_bytes() == before
