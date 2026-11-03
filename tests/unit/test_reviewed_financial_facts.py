import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.qa.evidence import validate_draft
from app.qa.facts import source_facts
from app.qa.financial import ReviewedFinancialFacts
from app.qa.reviews import ReviewedFacts
from app.qa.selection import select_target_facts
from app.qa.workflow import DocumentQA
from app.retrieval.chroma_store import resolve_query
from app.web.service import DemoEngine, generation_allowed

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "eval/results/answer_acceptance_v1_raw.jsonl"
RECORDS = [r for r in map(json.loads, RAW.read_text(encoding="utf-8").splitlines())
           if r["group"] == "financial_answerable"]


def example(index=1):
    record = deepcopy(RECORDS[index])
    source = record["response"]["sources"][0]
    return record, source


def draft(source, text=None):
    fact = source_facts(source, ReviewedFacts(ROOT))[0]
    return {"status": "answer", "claims": [{
        "text": text or f"2025年营业收入为{fact['value']}{fact['unit']}。",
        "stock_code": source["stock_code"], "report_period": "2025",
        "citations": [{"source_id": source["source_id"], "quote": fact["row_quote"]}],
    }]}


@pytest.mark.parametrize("index", range(4))
def test_four_selected_revenue_cells_bind_to_original_year_unit_and_scope(index):
    record, source = example(index)
    provider = ReviewedFacts(ROOT)
    assert source["layout_review_required"] and not source_facts(source)
    facts = source_facts(source, provider)
    assert len(facts) == 1 and facts[0]["entity_scope"] == "consolidated_group"
    _, checks = validate_draft(draft(source), [source], record["response"]["resolved"], provider,
                               require_reviewed_financial=True)
    assert checks["checks_passed"] and checks["selected_financial_cells_verified"]
    assert not checks["financial_row_column_semantics_verified"]
    assert not checks["generated_answer_accuracy_accepted"]
    assert source["layout_review_required"]


@pytest.mark.parametrize("text", [
    "2025年营业收入362,012,554千元",  # 同一行上年列
    "2025年营业收入423,701,834元",  # 千元误标元
    "2025年营业收入423,701.834百万元",  # 正确算术仍不支持换算
    "2024年营业收入423,701,834千元",
    "2025年母公司营业收入423,701,834千元",
    "2025年第一季度营业收入423,701,834千元",
    "2025年调整后营业收入423,701,834千元",
    "2025年营业总收入423,701,834千元",
])
def test_wrong_year_scale_entity_period_basis_and_metric_cannot_borrow_review(text):
    record, source = example()
    _, checks = validate_draft(draft(source, text), [source], record["response"]["resolved"],
                               ReviewedFacts(ROOT), require_reviewed_financial=True)
    assert not checks["checks_passed"]


@pytest.mark.parametrize("change", [
    "image", "pdf", "year_geometry", "value_geometry", "unit", "scope", "row_id", "page",
])
def test_review_provenance_and_column_coordinates_are_required(change):
    _, source = example(0)
    provider = ReviewedFinancialFacts(ROOT)
    provider.reviews = deepcopy(provider.reviews)
    r = provider.reviews[0]
    if change == "image":
        r["images"][0]["sha256"] = "0" * 64
    elif change == "pdf":
        r["pdf_sha256"] = "0" * 64
    elif change == "year_geometry":
        next(p for p in r["proofs"] if p["kind"] == "year")["bbox"][0] += 90
    elif change == "value_geometry":
        next(p for p in r["proofs"] if p["kind"] == "value")["bbox"][2] += 5
    elif change == "unit":
        r["unit"] = "千元"
    elif change == "scope":
        r["entity_scope"] = "parent"
    elif change == "row_id":
        r["row_index"] = 99
    else:
        source["physical_page"] += 1
    assert not provider.facts_for(source)


@pytest.mark.parametrize("question", [
    "宁德时代2025年营业收入同比增长多少？",
    "宁德时代2025年营业收入和净利润是多少？",
    "宁德时代2025年母公司营业收入多少？",
    "宁德时代2025年第二季度营业收入多少？",
    "宁德时代2025年营业收入换算成亿元是多少？",
    "宁德时代2025年营业收入扣除海外业务是多少？",
])
def test_unreviewed_requests_are_stopped_before_generation(question):
    scope = {("300750", "2025")}
    assert not generation_allowed(question, [], financial_scope=scope)


def test_requested_wrong_unit_and_unreviewed_same_page_row_remain_blocked():
    _, source = example()
    source["facts"] = source_facts(source, ReviewedFacts(ROOT))
    assert select_target_facts([source], resolve_query("宁德时代2025年营业收入多少元？"))[1][
        "status"] == "missing"
    assert not ReviewedFacts(ROOT).allows_citation(source, "归属于上市公司股东的净利润 72,201,282")


def test_plain_financial_parser_facts_do_not_authorize_generation_or_forged_catalog():
    _, source = example()
    source.update(content="单位：千元\n项目 2025年 2024年\n营业收入 200 100",
                  layout_review_required=False)
    source["facts"] = source_facts(source)
    assert source["facts"]
    resolved = resolve_query("宁德时代2025年营业收入多少千元？")
    assert select_target_facts([source], resolved)[1]["status"] == "missing"
    payload = {"status": "answer", "claims": [{
        "text": "2025年营业收入200千元", "stock_code": "300750", "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": source["content"]}],
    }]}
    assert not validate_draft(payload, [source], resolved, require_reviewed_financial=True)[1][
        "checks_passed"]


def test_legacy_business_rows_do_not_become_consolidated_revenue():
    source = {"source_id": "S1", "document_id": "600519-2025-annual", "stock_code": "600519",
              "report_period": "2025", "physical_page": 12, "document_type": "annual_report",
              "content": "酒类贸易业务收入 551.68 7,169.06 -92.30", "layout_review_required": True}
    assert not source_facts(source, ReviewedFacts(ROOT))


def test_workflow_recomputes_and_rejects_financial_forgery_after_preflight():
    record, source = example()
    qa = DocumentQA(None, None, supported_codes={"300750"}, supported_periods={"2025"},
                    review_provider=ReviewedFacts(ROOT))
    payload = draft(source, "2025年母公司营业收入423,701,834千元")
    result = qa.validate({"sources": [source], "resolved": record["response"]["resolved"],
                          "deployment": {}, "draft_payload": {
                              "draft": payload, "model": "test", "usage": {}}})["result"]
    assert result["mode"] == "validation_failed" and "claims" not in result


def test_financial_followup_resolves_company_without_inheriting_employee_metric():
    history = [{"role": "user", "content": "宁德时代2025年员工人数多少？"}]
    assert generation_allowed("那营业收入呢？", history, financial_scope={("300750", "2025")})
    assert not generation_allowed("那营业收入呢？", history, financial_scope={("000858", "2025")})


def test_output_guard_catches_revenue_alias_and_thousand_yuan_in_employee_answer():
    class FakeQA:
        def ask(self, question, history):
            return {"mode": "draft", "claims": [{"text": "2025年营收100千元"}]}

    engine = object.__new__(DemoEngine)
    engine.qa = FakeQA()
    result = engine.ask("宁德时代2025年员工人数多少？", [])
    assert result["mode"] == "evidence_only" and "claims" not in result
