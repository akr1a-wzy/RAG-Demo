from app.retrieval.hybrid import HybridHit
from app.retrieval.reranker import rerank_hits


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores

    def score(self, query, passages):
        assert query
        assert len(passages) == len(self.scores)
        return self.scores


def _hit(chunk_id: str, code: str, page: int) -> HybridHit:
    return HybridHit(
        chunk_id=chunk_id,
        parent_chunk_id=f"parent-{chunk_id}",
        text=f"text-{chunk_id}",
        stock_code=code,
        stock_name=code,
        report_period="2025",
        document_id=f"{code}-2025-annual",
        document_type="annual_report",
        title="2025年年度报告",
        page_start=page,
        page_end=page,
        source_url="https://example.com/report.pdf",
        similarity=0.5,
        distance=0.5,
        header_page=None,
        table_layout_review_required=False,
        bm25_score=None,
        rrf_score=0.01,
        vector_rank=1,
        keyword_rank=1,
    )


def test_reranker_orders_by_cross_encoder_score() -> None:
    hits = [_hit("a", "600519", 1), _hit("b", "600519", 2)]
    result = rerank_hits("营业收入", hits, FakeReranker([0.1, 0.9]), 1)
    assert result[0].chunk_id == "b"
    assert result[0].reranker_score == 0.9


def test_reranker_preserves_cross_company_quota() -> None:
    hits = [
        _hit("a", "600519", 1),
        _hit("b", "600519", 2),
        _hit("c", "000858", 1),
    ]
    result = rerank_hits(
        "比较营业收入",
        hits,
        FakeReranker([0.9, 0.8, 0.1]),
        2,
        ["600519", "000858"],
    )
    assert {hit.stock_code for hit in result} == {"600519", "000858"}


def test_explicit_financial_fact_prefers_primary_table_but_default_does_not() -> None:
    summary = _hit("summary", "600519", 9).model_copy(
        update={"primary_financial_evidence": True}
    )
    prose = _hit("prose", "600519", 20)
    assert rerank_hits("营业收入", [summary, prose], FakeReranker([0.1, 0.9]), 1)[0] == (
        prose.model_copy(update={"reranker_score": 0.9})
    )
    result = rerank_hits(
        "营业收入", [summary, prose], FakeReranker([0.1, 0.9]), 1,
        prefer_primary_financial=True,
    )
    assert result[0].chunk_id == "summary"


def test_direct_field_policy_keeps_labelled_fact_ahead_of_generic_prose() -> None:
    fact = _hit("fact", "000333", 77).model_copy(update={"direct_field_evidence": True})
    prose = _hit("prose", "000333", 78)
    result = rerank_hits(
        "在职员工数量合计", [fact, prose], FakeReranker([0.1, 0.9]), 1,
        prefer_direct_field=True,
    )
    assert result[0].chunk_id == "fact"
    assert rerank_hits(
        "在职员工数量合计", [fact, prose], FakeReranker([0.1, 0.9]), 1
    )[0].chunk_id == "prose"
