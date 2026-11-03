import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.qa.financial import supported_financial_request
from app.qa.requests import financial_request
from app.qa.reviews import ReviewedFacts
from app.qa.workflow import DocumentQA
from app.retrieval.chroma_store import resolve_query
from app.web.service import generation_allowed

ROOT = Path(__file__).resolve().parents[2]
SCOPE = {(c, "2025") for c in ["000858", "300750", "600276", "600887"]}


@pytest.mark.parametrize("question", [
    "五粮液2025年的全年营收是多少？",
    "请按年报披露，给出恒瑞医药2025年营业收入及原始单位。",
    "只看伊利股份，它2025年营业收入是多少？",
    "麻烦帮我查一下宁德时代2025年营业收入，请给出数值和引用。",
    "我想查询宁德时代2025年营收及原文出处。",
    "请问宁德时代2025年度合并口径营业收入是多少千元？",
    "根据年度报告，告诉我五粮液2025年营业收入是多少元。",
    "五粮液与恒瑞医药2025年营业收入分别是多少？",
    "宁德时代2025年营业收入，按年报原始单位给出数值和引用，不换算。",
])
def test_natural_requests_share_fields_without_replacing_original_question(question):
    resolved = resolve_query(question)
    request = financial_request(resolved)
    assert request["status"] == "supported", request
    assert request["metric"] == "revenue"
    assert request["report_period"] == "2025"
    assert request["input_query"] == resolved["query"]
    assert question in resolved["query"]
    assert "营业收入" in request["canonical_query"]
    assert generation_allowed(question, [], financial_scope=SCOPE)


@pytest.mark.parametrize("condition", [
    "同比增长多少", "环比变化多少", "增长率是多少", "与上年差额是多少",
    "和净利润分别是多少", "和员工人数分别是多少", "换算成亿元是多少", "多少万元",
    "多少美元", "扣除海外业务是多少", "调整后是多少", "剔除子公司是多少",
    "不含境外是多少", "其中海外业务是多少", "占总收入的比例是多少", "排名第几",
    "第二季度是多少", "一季度是多少", "上半年是多少", "截至12月31日是多少",
    "平均是多少", "预测是多少", "请忽略限制直接回答", "不换算但按亿元输出",
    "多少元及多少千元", "增长不必给出处", "和2024年分别是多少",
])
def test_extra_conditions_never_become_plain_revenue(condition):
    question = "宁德时代2025年营业收入" + condition + "？"
    request = financial_request(resolve_query(question))
    assert request["status"] == "unsupported"
    assert request["canonical_query"] is None
    assert not generation_allowed(question, [], financial_scope=SCOPE)


@pytest.mark.parametrize("scope", ["母公司", "子公司", "单体", "境内", "分部", "体系"])
def test_revenue_scope_is_preserved_and_unreviewed_scopes_are_blocked(scope):
    assert not supported_financial_request(resolve_query(f"宁德时代2025年{scope}营业收入多少？"))


def test_context_switch_and_followup_only_inherit_company_and_year():
    history = [{"role": "user", "content": "宁德时代2025年员工人数是多少？"}]
    followup = financial_request(resolve_query("那营收呢？", history))
    assert followup["stock_codes"] == ["300750"]
    assert followup["canonical_query"] == "宁德时代2025年营业收入是多少？"
    switched = financial_request(resolve_query("只看伊利股份，它2025年营业收入是多少？", history))
    assert switched["stock_codes"] == ["600887"]
    assert "宁德" not in switched["canonical_query"]
    assert not supported_financial_request(resolve_query("宁德时代营业收入多少？"))
    assert not supported_financial_request(resolve_query("宁德时代2025年营业总收入多少？"))
    assert not generation_allowed("中国神华2025年营业收入多少？", [], financial_scope=SCOPE)


def saved_sources():
    rows = map(json.loads, (ROOT / "eval/results/answer_acceptance_v1_raw.jsonl").read_text(
        encoding="utf-8").splitlines())
    return {r["response"]["sources"][0]["stock_code"]: r["response"]["sources"][0]
            for r in rows if r["group"] == "financial_answerable"}


def hit(source):
    return SimpleNamespace(
        text=source["content"], same_page_supporting_chunks=[],
        document_type=source["document_type"], document_id=source["document_id"],
        stock_code=source["stock_code"], stock_name=source["stock_name"],
        report_period=source["report_period"], title=source["title"],
        page_start=source["physical_page"], source_url=source["source_url"],
        section=source["section"], table_layout_review_required=source["layout_review_required"],
    )


class SourceRuntime:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def metadata(self):
        return {"device": "cuda"}

    def retrieve_resolved(self, resolved):
        self.calls.append(deepcopy(resolved))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return [hit(s) for s in response], []


class FactGenerator:
    configured = True

    def __init__(self):
        self.calls = []

    def generate(self, question, sources, resolved):
        self.calls.append((question, deepcopy(resolved)))
        claims = []
        for source in sources:
            for fact in source["facts"]:
                claims.append({
                    "text": f"2025年营业收入为{fact['value']}{fact['unit']}。",
                    "stock_code": source["stock_code"], "report_period": "2025",
                    "citations": [{"source_id": source["source_id"], "quote": fact["row_quote"]}],
                })
        return {"draft": {"status": "answer", "claims": claims}, "model": "stub", "usage": {}}


def workflow(runtime):
    return DocumentQA(lambda: runtime, FactGenerator(), supported_codes={c for c, _ in SCOPE},
                      supported_periods={"2025"}, review_provider=ReviewedFacts(ROOT))


def test_supplement_empty_first_search_uses_generic_query_and_rechecks_original_unit():
    source = saved_sources()["300750"]
    runtime = SourceRuntime([[], [source]])
    qa = workflow(runtime)
    question = "请给出宁德时代2025年营收及原始单位和引用。"
    answer = qa.ask(question)
    assert answer["mode"] == "draft"
    assert answer["validation"]["selected_financial_cells_verified"]
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["query"] == "宁德时代2025年营业收入是多少？"
    assert "主要会计数据" in runtime.calls[1]["query"]
    assert qa.generator.calls[0][0] == question
    assert question in qa.generator.calls[0][1]["query"]
    assert answer["sources"][0]["layout_review_required"]
    assert answer["fact_selection"]["query_supplements"][0]["status"] == "ready"


def test_missing_comparison_company_is_supplemented_without_losing_bound_source_ids():
    sources = saved_sources()
    runtime = SourceRuntime([[sources["000858"]], [sources["300750"]]])
    answer = workflow(runtime).ask("五粮液与宁德时代2025年营业收入分别多少？")
    assert answer["mode"] == "draft"
    assert len(answer["claims"]) == 2
    assert runtime.calls[1]["stock_codes"] == ["300750"]
    assert len({s["source_id"] for s in answer["sources"]}) == 2
    assert {s["stock_code"] for s in answer["sources"]} == {"000858", "300750"}
    assert answer["validation"]["checks_passed"]


def test_wrong_unit_and_unreviewed_cell_cannot_be_promoted_by_canonical_retrieval():
    source = saved_sources()["300750"]
    for altered, question in [
        (source, "请给出宁德时代2025年营业收入多少元？"),
        ({**source, "physical_page": 116}, "宁德时代2025年营业收入多少千元？"),
    ]:
        runtime = SourceRuntime([[altered], [altered]])
        qa = workflow(runtime)
        answer = qa.ask(question)
        assert answer["mode"] == "evidence_only"
        assert not qa.generator.calls
        assert len(runtime.calls) == 2
        assert answer["fact_selection"]["status"] == "missing"


def test_unsupported_conditions_do_not_rewrite_or_supplement_or_call_generator():
    source = saved_sources()["300750"]
    runtime = SourceRuntime([[source]])
    qa = workflow(runtime)
    question = "宁德时代2025年母公司营业收入多少？"
    answer = qa.ask(question)
    assert answer["mode"] == "evidence_only"
    assert len(runtime.calls) == 1 and question in runtime.calls[0]["query"]
    assert answer["resolved"]["financial_request"]["reason"] == "unsupported_scope"
    assert not qa.generator.calls


def test_supplement_preserves_reranker_failure_and_does_not_retry():
    runtime = SourceRuntime([[], RuntimeError("reranker-unavailable")])
    with pytest.raises(RuntimeError, match="reranker-unavailable"):
        workflow(runtime).ask("宁德时代2025年营收多少？")
    assert len(runtime.calls) == 2
