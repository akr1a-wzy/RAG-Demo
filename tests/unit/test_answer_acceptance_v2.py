import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "v2_once_tests", ROOT / "scripts/evaluate_answers_v2_once.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_attempt_is_immutable_and_hash_changes_fail_before_requests(runner, tmp_path):
    state = tmp_path / "state.json"
    runner.reserve_attempt(state, {"attempt_count": 1})
    with pytest.raises(FileExistsError):
        runner.reserve_attempt(state, {"attempt_count": 0})
    assert json.loads(state.read_text(encoding="utf-8"))["attempt_count"] == 1
    with pytest.raises(ValueError, match="冻结文件变化"):
        runner.verify_freeze({"file_hashes": {"src/app/qa/facts.py": "0" * 64}})


def test_financial_subgroup_can_fail_even_if_overall_answer_rate_passes(runner):
    contract = json.loads((ROOT / "eval/answer_acceptance_v2_contract.json").read_text(
        encoding="utf-8"))
    gold = {"facts": []}
    cases, records = [], []
    for i in range(25):
        code = f"{i:06d}"
        fact = {"fact_id": str(i), "stock_code": code, "stock_name": f"合成公司{chr(65 + i)}",
                "period": "2025", "metric": "headcount_total", "value": "100", "unit": "人",
                "document_id": str(i), "physical_page": 1, "row_label": "员工人数"}
        gold["facts"].append(fact)
        case = {"id": str(i), "group": "new_staff_single", "expected_mode": "draft",
                "fact_ids": [str(i)]}
        answer = {"mode": "draft", "resolved": {"stock_codes": [code], "report_period": "2025"},
                  "sources": [{"source_id": "S1", "document_id": str(i), "physical_page": 1,
                               "content": "员工人数100人"}],
                  "claims": [{"text": "2025年员工人数100人", "stock_code": code,
                              "report_period": "2025", "citations": [
                                  {"source_id": "S1", "quote": "员工人数100人"}]}]}
        cases.append(case)
        records.append({"id": str(i), "response": answer, "http_seconds": 0.01})
    for i in range(2):
        key = f"f{i}"
        gold["facts"].append({**gold["facts"][i], "fact_id": key, "metric": "revenue",
                              "unit": "元", "row_label": "营业收入"})
        cases.append({"id": key, "group": "financial_answerable", "expected_mode": "draft",
                      "fact_ids": [key]})
        records.append({"id": key, "http_seconds": 0.01, "response": {
            "mode": "evidence_only", "claims": [], "sources": [], "resolved": {
                "stock_codes": [f"{i:06d}"], "report_period": "2025"}}})
    report = runner.aggregate_v2(records, cases, gold, contract)
    assert report["gates"]["document_answer_90"]
    assert not report["gates"]["financial_answer_90"] and not report["gate_passed"]


def test_incomplete_case_batch_never_passes_gate(runner):
    contract = json.loads((ROOT / "eval/answer_acceptance_v2_contract.json").read_text(
        encoding="utf-8"))
    report = runner.aggregate_v2([], [{"id": "missing", "group": "routing_control",
                                      "expected_mode": "refused", "fact_ids": []}],
                                 {"facts": []}, contract)
    assert not report["gate_passed"] and report["completed_case_count"] == 0
