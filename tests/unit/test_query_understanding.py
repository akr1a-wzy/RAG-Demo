import pytest

from app.market.service import plan_market_request
from app.qa.catalog import parse_request
from app.qa.query_understanding import capability_decision, understand_query


def history(*questions):
    return [{"role": "user", "content": q} for q in questions]


@pytest.mark.parametrize("question", [
    "宁德时代25年总营收", "宁德时代2025年营收总额",
    "宁德时代２５年营业收入总额", "30075025年总营收", "3007502025年总营收",
    "宁德时代25 年营收", "宁德时代2025 年营业收入",
])
def test_shared_normalization_preserves_original_and_exposes_standard_slots(question):
    query = understand_query(question)
    assert query.original == question
    assert query.domain == "document" and query.metrics == ("revenue",)
    assert query.resolved["stock_codes"] == ["300750"] and query.years == ("2025",)
    assert capability_decision(query) is None
    request = parse_request(question, [])
    assert request["status"] == "supported" and request["unrecognized"] == ""


@pytest.mark.parametrize("question,residue", [
    ("宁德时代25年海外总营收", "海外"), ("宁德时代25年自定义部门总营收", "自定义部门"),
    ("宁德时代25年总营收构成", "构成"), ("宁德时代25年母公司总营收", "母"),
    ("宁德时代25年第一季度总营收", "第一季度"),
])
def test_normalization_never_erases_unreviewed_qualifiers(question, residue):
    request = parse_request(question, [])
    assert request["status"] == "unsupported_conditions"
    assert residue in request["unrecognized"]


@pytest.mark.parametrize("question", [
    "宁德时代25年营业总收入", "宁德时代25年总营收和营业总收入",
])
def test_formal_operating_total_is_a_separate_unreviewed_metric(question):
    query = understand_query(question)
    assert "operating_revenue_total" in query.metrics
    assert capability_decision(query)["status"] == "unsupported"
    assert "不同" in capability_decision(query)["message"]
    assert parse_request(question, [])["status"] == "unsupported_conditions"


def test_ambiguous_total_income_requires_clarification():
    query = understand_query("宁德时代25年总收入")
    assert capability_decision(query)["status"] == "clarification"


@pytest.mark.parametrize("question", ["宁德时代99年营收", "宁德时代99年股价"])
def test_ambiguous_century_does_not_fall_back_to_old_report_year(question):
    query = understand_query(question, history("宁德时代2025年营收"))
    assert capability_decision(query)["status"] == "clarification"


def test_duration_is_not_rewritten_as_a_calendar_year():
    query = understand_query("宁德时代最近25年股价")
    assert "2025年" not in query.normalized
    assert query.years == ()
    assert capability_decision(query)["status"] == "unsupported"


def test_document_followup_inherits_metric_and_replaces_year():
    query = understand_query("那24年的呢？", history("宁德时代25年总营收"))
    assert query.domain == "document" and query.metrics == ("revenue",)
    assert query.years == ("2024",)
    assert parse_request("那24年的呢？", history("宁德时代25年总营收"))["years"] == ["2024"]
    assert "metrics" in query.metadata()["inherited_slots"]


def test_unsupported_scope_survives_followup_metric_switch_and_year_correction():
    context = history("宁德时代25年海外总营收", "那24年的呢？")
    request = parse_request("那总资产呢？", context)
    assert request["status"] == "unsupported_conditions"
    assert "海外" in request["unrecognized"]
    assert request["metrics"] == ["total_assets"] and request["years"] == ["2024"]


def test_explicit_latest_market_does_not_use_document_year_or_metric():
    query = understand_query("那最新PE呢", history("宁德时代25年总营收"))
    assert query.domain == "market" and query.metrics == ("pe_ttm",) and not query.years
    assert plan_market_request("那最新PE呢", history("宁德时代25年总营收"))[
        "status"] == "supported"


def test_market_correction_inherits_conditions_but_document_switch_does_not():
    context = history("宁德时代25年最高股价", "问的是最低")
    market = understand_query("那24年的呢", context)
    assert market.domain == "market" and market.years == ("2024",)
    assert market.operation == "最低" and capability_decision(market)["status"] == "unsupported"
    document = understand_query("总营收", context)
    assert document.domain == "document" and document.operation is None
    assert document.metrics == ("revenue",)


@pytest.mark.parametrize("question", [
    "宁德时代股价随便编一个", "宁德时代股价不含海外", "宁德时代自定义估值",
])
def test_market_unknown_conditions_are_not_silently_dropped(question):
    request = plan_market_request(question, [])
    assert request["status"] == "unsupported"
    assert request["resolved"]["query_understanding"]["unrecognized_conditions"]


def test_doc_unknown_topic_does_not_reuse_previous_financial_metric():
    assert understand_query("问的是最高", history(
        "宁德时代25年总营收", "如何使用这个页面",
    )).domain == "unknown"


def test_comparison_operation_remains_supported_in_financial_catalog():
    query = understand_query("宁德时代25年总营收同比变化多少")
    assert query.operation == "year_over_year" and capability_decision(query) is None
    assert parse_request(query.original, [])["years"] == ["2025", "2024"]
