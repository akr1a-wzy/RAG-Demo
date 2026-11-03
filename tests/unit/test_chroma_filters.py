import pytest

from app.retrieval.chroma_store import build_filter, resolve_query


def test_filters_combine_stock_year_and_type() -> None:
    assert build_filter() is None
    result = build_filter(["茅台", "600519"], "2025", "annual_report")
    assert result == {
        "$and": [
            {"stock_code": {"$in": ["600519"]}},
            {"report_period": {"$eq": "2025"}},
            {"document_type": {"$eq": "annual_report"}},
        ]
    }


def test_followup_uses_user_history_and_current_stock_wins() -> None:
    history = [
        {"role": "user", "content": "五粮液2025年营业收入是多少？"},
        {"role": "assistant", "content": "不要从回答里的茅台继承公司"},
    ]
    result = resolve_query("它的净利润呢？", history)
    assert result["stock_codes"] == ["000858"]
    assert result["report_period"] == "2025"
    switched = resolve_query("泸州老窖呢？", history)
    assert switched["stock_codes"] == ["000568"]


def test_comparison_resolves_three_companies() -> None:
    resolved = resolve_query("比较贵州茅台、五粮液和泸州老窖2025年营业收入")
    assert resolved["stock_codes"] == ["600519", "000858", "000568"]
    assert resolved["report_period"] == "2025"


def test_resolves_new_companies_and_uses_canonical_context() -> None:
    resolved = resolve_query("比较招行和中石油2025年营业收入")
    assert resolved["stock_codes"] == ["600036", "601857"]
    assert resolved["report_period"] == "2025"
    assert resolved["query"].startswith("招商银行、中国石油 2025年")


def test_unknown_company_filter_is_not_silently_removed() -> None:
    with pytest.raises(ValueError):
        build_filter(["不存在的公司"])
