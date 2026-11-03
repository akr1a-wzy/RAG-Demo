import copy
from pathlib import Path

import pytest

from app.qa.catalog import LABELS, MONEY_METRICS, DocumentCatalog, parse_request, verify_facts

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def catalog():
    return DocumentCatalog(ROOT)


def test_original_pdf_proofs_and_complete_financial_matrix(catalog):
    assert catalog.audit["passed"]
    assert len(catalog.facts) == 403
    keys = [
        (f["stock_code"], f["period"], f["metric"])
        for f in catalog.facts
        if f["metric"] in MONEY_METRICS
    ]
    assert len(keys) == len(set(keys)) == 320
    assert len(catalog.coverage()) == 20
    # 这是目录/路由覆盖检查，期望值取自目录，不能作为独立答案准确率。
    for code, year, metric in keys:
        r = catalog.ask(f"{code}{year}年{LABELS[metric]}多少？", [])
        assert r["mode"] == "structured_answer", r
        assert len(r["claims"]) == 1
        assert r["validation"]["catalog_proofs_verified"]
        assert not r["usage"] and r["answer_method"] == "structured_facts"


@pytest.mark.parametrize("question", [
    "总营收", "25年总营收", "宁德时代2025年总营收", "30075025年总营收",
])
def test_reported_revenue_aliases_use_verified_values_with_explicit_basis(catalog, question):
    history = [{"role": "user", "content": "宁德时代2025年最高股价"},
               {"role": "user", "content": "今日收盘价"}]
    answer = catalog.ask(question, history)
    control = catalog.ask("宁德时代2025年营业收入", [])
    assert answer["mode"] == "structured_answer"
    assert answer["claims"] == control["claims"] and answer["sources"] == control["sources"]
    assert "营业收入口径" in answer["message"].replace("“", "").replace("”", "")
    assert not answer["usage"]


def test_revenue_followup_and_unit_change_keep_verified_values(catalog):
    history = [{"role": "user", "content": "宁德时代25年总营收多少亿元"}]
    response = catalog.ask("那24年换算万元呢", history)
    control = catalog.ask("宁德时代2024年营业收入多少万元", [])
    assert response["mode"] == "structured_answer"
    assert response["claims"] == control["claims"]


def test_demo_uses_shared_understanding_without_llm_or_market_provider(catalog):
    from app.web.service import DemoEngine

    class Forbidden:
        def ask(self, *args, **kwargs):
            pytest.fail("Revenue aliases must use verified catalog, not providers or generation")

    engine = object.__new__(DemoEngine)
    engine.catalog = catalog
    engine.market = engine.qa = engine.evidence_qa = Forbidden()
    history = [{"role": "user", "content": "宁德时代25年最高股价"}]
    for question in ["总营收", "25年总营收", "宁德时代2025年总营收", "那24年的呢"]:
        result = engine.ask(question, history)
        assert result["mode"] == "structured_answer" and not result["usage"]
        history.append({"role": "user", "content": question})


def test_decimal_yoy_restated_and_scope(catalog):
    r = catalog.ask("中国神华2025年营收是多少，同比变化多少？", [])
    assert r["validation"]["calculations"][0]["result"] == "-13.21"
    assert "重述后" in r["claims"][1]["text"]
    assert r["sources"][0]["report_period"] == "2025"
    assert any(f["period"] == "2024" for s in r["sources"] for f in s["facts"])


def test_unit_conversion_company_comparison_and_roe(catalog):
    r = catalog.ask("茅台和五粮液2025年营收相差多少亿元？", [])
    assert r["validation"]["calculations"][0]["result"] == "1283.10"
    assert "1688.3810251479亿元" in r["claims"][0]["text"]
    r = catalog.ask("隆基2025年ROE同比变化多少？", [])
    assert r["validation"]["calculations"][0]["result"] == "1.92"
    assert r["validation"]["calculations"][0]["unit"] == "个百分点"
    r = catalog.ask("隆基2025年归母净利润同比多少？", [])
    assert not r["validation"]["calculations"]
    assert "基数非正" in r["claims"][-1]["text"]


def test_announcement_dates_not_report_year_and_original_share_basis(catalog):
    r = catalog.ask("五粮液2025年分红及股权登记日是多少？", [])
    text = "".join(c["text"] for c in r["claims"])
    assert "2026-07-15" in text and "每10股派25.78元" in text
    assert "每1股派2.5796852元" in text
    r = catalog.ask("茅台2025年每10股分红多少？", [])
    assert "每10股派279.930元" in r["claims"][0]["text"]
    r = catalog.ask("宝钢2025年全年分红多少？", [])
    assert len(r["claims"]) == 1 and "全年预计" in r["claims"][0]["text"]
    r = catalog.ask("宝钢2025年下半年分红多少？", [])
    assert len(r["claims"]) == 1 and "0.18元" in r["claims"][0]["text"]


def test_announcement_missing_is_not_zero_or_inferred_date(catalog):
    r = catalog.ask("招商银行2025年实施分红的股权登记日是多少？", [])
    assert r["mode"] == "refused" and not r.get("claims")
    r = catalog.ask("隆基2025年分红多少？", [])
    assert "不进行利润分配" in r["claims"][0]["text"]
    r = catalog.ask("隆基2025年实施分红多少？", [])
    assert r["mode"] == "refused"


def test_followup_and_employee_routing(catalog):
    history = [{"role": "user", "content": "宁德2025年营收多少？"}]
    r = catalog.ask("那2024年呢？", history)
    assert r["mode"] == "structured_answer" and r["claims"][0]["report_period"] == "2024"
    history.append({"role": "user", "content": "那2024年呢？"})
    r = catalog.ask("那换算亿元呢？", history)
    assert r["mode"] == "structured_answer" and "亿元" in r["claims"][0]["text"]
    assert catalog.ask("宝钢2025年员工总人数及原文呢？", history) is None
    r = catalog.ask("中国平安2025年总资产多少？", [], evidence_only=True)
    assert r["mode"] == "evidence_only" and not r.get("claims")


@pytest.mark.parametrize(
    "prefix,suffix",
    [("请告诉我", "附原文"), ("麻烦查一下", "并给我原文"), ("请问", "以及原文"), ("", "")],
)
def test_polite_financial_questions(prefix, suffix):
    assert parse_request(f"{prefix}中国石油2025年总资产多少？{suffix}", [])["status"] == "supported"


@pytest.mark.parametrize(
    "q",
    [
        "茅台2025年母公司营业收入是多少？",
        "茅台2025年第一季度营收多少？",
        "茅台2025年营业收入美元是多少？",
        "茅台2025年营业收入不含海外是多少？",
        "茅台2025年营业收入增长原因？",
        "茅台2025年稀释每股收益多少？",
        "茅台2025年净利润和营收是多少？",
        "茅台2025年营收和营业总收入是多少？",
        "神华2024年调整前营收多少？",
        "茅台2025年税后分红是多少？",
        "茅台2025年营收占比多少？",
        "宝钢2025年自定义部门营收是多少？",
    ],
)
def test_unknown_conditions_not_stripped(q):
    assert parse_request(q, [])["status"] == "unsupported_conditions"


@pytest.mark.parametrize("kind", ["value", "year", "unit", "image", "pdf", "date", "share"])
def test_corrupted_fact_proofs_fail_closed(catalog, kind):
    if kind in {"date", "share"}:
        f = copy.deepcopy(
            next(
                f
                for f in catalog.facts
                if f["metric"] == ("record_date" if kind == "date" else "dividend_gross")
            )
        )
        f["value" if kind == "date" else "share_basis"] = "2027-01-01" if kind == "date" else 10
    else:
        f = copy.deepcopy(next(f for f in catalog.facts if f["metric"] == "revenue"))
        if kind == "value":
            other = next(
                f
                for f in catalog.facts
                if f["metric"] == "revenue"
                and f["stock_code"] == "600519"
                and f["period"] == "2024"
            )
            f["value"] = other["value"]
            f["proofs"][1] = copy.deepcopy(other["proofs"][1])
        elif kind == "year":
            f["period"] = "2023"
        elif kind == "unit":
            f["unit"] = "百万元"
        elif kind == "image":
            f["visual_evidence"][0]["sha256"] = "bad"
        else:
            f["pdf_sha256"] = "bad"
    assert not verify_facts(ROOT, [f], catalog.documents)["passed"]
