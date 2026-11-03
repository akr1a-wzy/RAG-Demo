import pytest

from app.retrieval.hybrid import (
    BM25Index,
    HybridConfig,
    HybridHit,
    deduplicate_page_hits,
    financial_section_boost,
    is_financial_summary_heading,
    rrf_fuse,
    tokenize,
)


def test_chinese_bigrams_and_numeric_terms() -> None:
    assert tokenize("营业收入2025年EPS") == ["营业", "业收", "收入", "2025", "年", "eps"]


def test_bm25_finds_precise_field_and_respects_filters() -> None:
    index = BM25Index(
        {"revenue": "营业收入 净利润", "risk": "经营风险 渠道建设", "other": "其他公司营业收入"}
    )
    hits = index.search("营业收入", {"revenue", "risk"}, 5)
    assert [chunk_id for chunk_id, _ in hits] == ["revenue"]
    assert index.search("完全无匹配的术语", {"revenue"}, 5) == []
    assert index.search("营业收入", set(), 5) == []


def test_rrf_uses_ranks_and_does_not_count_duplicate_ids_twice() -> None:
    result = rrf_fuse([["a", "b"], ["b", "c"]])
    assert result[0][0] == "b"
    assert result[0][1] == pytest.approx(1 / 62 + 1 / 61)
    assert rrf_fuse([["a", "a"]]) == [("a", 1 / 61)]
    assert rrf_fuse([]) == []


@pytest.mark.parametrize(
    "kwargs", [{"candidate_k": 0}, {"rrf_k": 0}, {"bm25_k1": 0}, {"bm25_b": 1.1}]
)
def test_invalid_hybrid_config(kwargs) -> None:
    with pytest.raises(ValueError):
        HybridConfig(**kwargs)


def test_financial_section_boost_is_scoped() -> None:
    assert financial_section_boost("营业收入同比变化", "六、主要会计数据", 0.01) == 0.01
    assert financial_section_boost("营业收入同比变化", "风险因素", 0.01) == 0.0
    assert financial_section_boost("公司地址", "六、主要会计数据", 0.01) == 0.0
    with pytest.raises(ValueError):
        HybridConfig(section_boost=-0.01)


def test_summary_heading_rejects_subsidiary_and_quarterly_tables() -> None:
    assert is_financial_summary_heading("七、近三年主要会计数据和财务指标")
    assert is_financial_summary_heading("2.1 本集团主要会计数据和财务指标")
    assert is_financial_summary_heading("财务摘要")
    assert not is_financial_summary_heading("1. 财务公司主要财务指标")
    assert not is_financial_summary_heading("八、分季度主要财务指标")
    assert not is_financial_summary_heading("有关主要会计数据请参见第二节")


def _hit(chunk_id: str, page: int, score: float) -> HybridHit:
    return HybridHit(
        chunk_id=chunk_id,
        parent_chunk_id=f"parent-{chunk_id}",
        text=f"text-{chunk_id}",
        stock_code="600519",
        stock_name="贵州茅台",
        report_period="2025",
        document_id="600519-2025-annual",
        document_type="annual_report",
        title="2025年年度报告",
        page_start=page,
        page_end=page,
        source_url="https://example.com/report.pdf",
        similarity=score,
        distance=1 - score,
        header_page=None,
        table_layout_review_required=False,
        bm25_score=None,
        rrf_score=score,
        vector_rank=1,
        keyword_rank=None,
    )


def test_page_dedup_keeps_best_rank_and_attaches_supporting_chunks() -> None:
    hits = deduplicate_page_hits([_hit("a", 9, 0.9), _hit("b", 9, 0.8), _hit("c", 20, 0.7)])
    assert [hit.page_start for hit in hits] == [9, 20]
    assert hits[0].chunk_id == "a"
    assert [item.chunk_id for item in hits[0].same_page_supporting_chunks] == ["b"]


def test_page_dedup_preserves_direct_field_flag_from_supporting_chunk() -> None:
    prose = _hit("prose", 9, 0.9)
    field = _hit("field", 9, 0.8).model_copy(update={"direct_field_evidence": True})
    assert deduplicate_page_hits([prose, field])[0].direct_field_evidence
