import pytest

from app.market.service import MarketService, plan_market_request
from app.web.service import DemoEngine


def history(*questions):
    return [{"role": "user", "content": q} for q in questions]


@pytest.mark.parametrize("question", [
    "宁德时代25年股价最高是多少", "宁德时代2025年股价最高是多少",
    "宁德时代25 年股价最低是多少", "宁德时代2024 年收盘价最高是多少",
    "宁德时代股价最高是多少", "宁德时代股价最低是多少",
    "宁德时代最近20个交易日股价最大值", "宁德时代市值峰值是多少",
    "宁德时代PE最高是多少", "宁德时代价格低点是多少",
])
def test_extreme_requests_never_silently_return_latest_quote(question):
    request = plan_market_request(question, [])
    assert request["status"] == "unsupported"
    assert "不能用最新报价代替" in request["message"]


@pytest.mark.parametrize("question", [
    "宁德时代25年股价是多少", "宁德时代2025 年股价是多少",
    "宁德时代25 年收盘价", "宁德时代2025年日K",
])
def test_explicit_short_and_spaced_years_are_not_latest_quotes(question):
    request = plan_market_request(question, [])
    assert request["status"] == "unsupported"
    assert request["resolved"]["report_period"] == "2025"


@pytest.mark.parametrize("followup", [
    "问的是最高", "我问的是最高", "那最低呢？", "最高是多少？",
    "我要的是最低", "2024年呢", "那24年的呢？",
])
def test_corrective_followup_preserves_market_domain_and_constraints(followup):
    request = plan_market_request(followup, history(
        "宁德时代2025年财务概览及原文", "宁德时代25年股价最高是多少",
    ))
    assert request["status"] == "unsupported"
    assert request["resolved"]["stock_codes"] == ["300750"]
    assert "不能用最新报价代替" in request["message"]
    if "24" in followup:
        assert request["resolved"]["market_intent"]["years"] == ["2024"]
        assert "2025" not in request["message"]
    else:
        assert "2025" in request["message"]


def test_multiple_corrections_preserve_domain_and_override_operation():
    request = plan_market_request("那最低呢？", history(
        "宁德时代25年股价是多少", "问的是最高", "那24年的呢？",
    ))
    assert request["status"] == "unsupported"
    assert request["resolved"]["market_intent"]["years"] == ["2024"]
    assert request["resolved"]["market_intent"]["operation"] == "最低"


@pytest.mark.parametrize("question", ["那PE呢", "那PB呢", "那股价呢", "那2024年PE呢"])
def test_metric_followup_keeps_annual_constraints_unless_latest_is_explicit(question):
    request = plan_market_request(question, history("宁德时代25年股价最高是多少"))
    assert request["status"] == "unsupported"
    assert request["resolved"]["market_intent"]["years"] == [
        "2024" if "2024" in question else "2025"
    ]


@pytest.mark.parametrize("preceding", [
    "宁德时代2025年财务概览及原文", "宁德时代25年股价最高是多少",
])
@pytest.mark.parametrize("question", ["那最新PE呢", "宁德时代最新股价是多少"])
def test_explicit_latest_request_drops_previous_domain_dates_and_extremes(preceding, question):
    request = plan_market_request(question, history(preceding))
    assert request["status"] == "supported" and request["kind"] == "quote"
    assert request["resolved"]["market_intent"]["years"] == []
    assert request["resolved"]["market_intent"]["operation"] is None


@pytest.mark.parametrize("question", [
    "宁德时代2025年营业收入最高是多少", "问的是最高", "那2024年呢",
])
def test_document_topic_does_not_become_market_followup(question):
    assert plan_market_request(question, history(
        "宁德时代股价是多少", "宁德时代2025年营业收入是多少",
    )) is None


def test_unknown_topic_breaks_corrective_inheritance():
    assert plan_market_request("问的是最高", history(
        "宁德时代25年股价最高是多少", "如何使用这个页面",
    )) is None


def test_bare_extreme_without_context_asks_for_metric_and_period():
    request = plan_market_request("最高？", [])
    assert request["status"] == "clarification"
    assert "指标" in request["message"]


@pytest.mark.parametrize("question", [
    "宁德时代明年股价最高是多少", "宁德时代明天最高股价会涨吗",
])
def test_forecast_refusal_takes_priority_over_extreme_capability(question):
    assert plan_market_request(question, [])["status"] == "refused"


@pytest.mark.parametrize("question", [
    "宁德时代2025年股价最高及营业收入", "宁德时代股价和分红是多少",
])
def test_mixed_document_and_market_conditions_are_not_dropped(question):
    assert plan_market_request(question, [])["status"] == "clarification"


@pytest.mark.parametrize("replay", [False, True])
def test_real_demo_router_blocks_original_and_followups_without_retrieval_or_provider(
    tmp_path, monkeypatch, replay,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported extreme invoked provider, replay, document or generation")

    class ForbiddenQA:
        ask = forbidden

    engine = object.__new__(DemoEngine)
    engine.market = MarketService(tmp_path)
    engine.catalog = engine.qa = engine.evidence_qa = ForbiddenQA()
    monkeypatch.setattr(engine.market, "fetch", forbidden)
    monkeypatch.setattr(engine.market.store, "load", forbidden)
    context = history("宁德时代2025年财务概览及原文")
    for question in ["宁德时代25年股价最高是多少", "问的是最高", "那最低呢？"]:
        answer = engine.ask(question, context, market_replay=replay)
        assert answer["mode"] == "unsupported"
        assert not answer.get("sources") and not answer.get("market")
        assert "不能用最新报价代替" in answer["message"]
        context.extend(history(question))
