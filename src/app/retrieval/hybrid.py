"""BM25与向量双路召回，RRF按排名融合，不直接相加不同分数。"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from app.retrieval.chroma_store import ChromaIndex, SearchHit
from app.retrieval.embedding_prepare import EmbeddingChunk
from app.retrieval.field_evidence import is_direct_field_evidence
from app.retrieval.pdf_extract import file_sha256
from app.tools.ticker import normalize_ticker

HYBRID_VERSION = "bm25-bigrams-rrf-v1"
FINANCIAL_QUERY_TERMS = (
    "营业收入",
    "营业总收入",
    "净利润",
    "利润总额",
    "营业利润",
    "现金流量净额",
    "每股收益",
    "总资产",
    "资产总额",
)
FINANCIAL_SECTION_TERMS = ("主要会计数据", "主要财务数据", "主要财务指标")


def is_financial_summary_heading(text: str) -> bool:
    for line in text.splitlines():
        compact = re.sub(r"\s+", "", line)
        compact = re.sub(
            r"^(?:[一二三四五六七八九十百]+[、．.]|[（(][一二三四五六七八九十\d]+[）)]"
            r"|\d+(?:\.\d+)*[、．.]?)", "", compact,
        )
        compact = compact.removeprefix("近三年").removeprefix("本集团")
        if compact.startswith("主要会计数据") or compact in {
            "主要财务指标", "主要财务数据", "财务摘要",
        }:
            return True
    return False


def tokenize(text: str) -> list[str]:
    """中文连续字串用二元字组，英文和数字保留完整词；无额外分词依赖。"""
    terms = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+|\d+(?:\.\d+)?", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            terms.extend(
                [part] if len(part) == 1 else [part[i : i + 2] for i in range(len(part) - 1)]
            )
        else:
            terms.append(part)
    return terms


@dataclass(frozen=True)
class HybridConfig:
    candidate_k: int = 20
    rrf_k: int = 60
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    deduplicate_pages: bool = False
    section_boost: float = 0.0

    def __post_init__(self) -> None:
        if self.candidate_k < 1 or self.rrf_k < 1 or self.bm25_k1 <= 0:
            raise ValueError("candidate_k、rrf_k、bm25_k1必须为正")
        if not 0 <= self.bm25_b <= 1:
            raise ValueError("bm25_b必须在0到1之间")
        if self.section_boost < 0:
            raise ValueError("section_boost不能为负")


class BM25Index:
    def __init__(self, texts: dict[str, str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.lengths = {}
        self.postings: dict[str, dict[str, int]] = defaultdict(dict)
        for chunk_id, text in texts.items():
            terms = Counter(tokenize(text))
            self.lengths[chunk_id] = sum(terms.values())
            for term, frequency in terms.items():
                self.postings[term][chunk_id] = frequency
        self.size = len(texts)
        self.average_length = sum(self.lengths.values()) / max(1, self.size)

    def search(self, query: str, allowed_ids: set[str], top_k: int) -> list[tuple[str, float]]:
        if top_k < 1:
            raise ValueError("top_k必须为正")
        scores: dict[str, float] = defaultdict(float)
        if not allowed_ids or not self.average_length:
            return []
        for term in set(tokenize(query)):
            postings = self.postings.get(term, {})
            idf = math.log(1 + (self.size - len(postings) + 0.5) / (len(postings) + 0.5))
            for chunk_id, frequency in postings.items():
                if chunk_id not in allowed_ids:
                    continue
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * self.lengths[chunk_id] / self.average_length
                )
                scores[chunk_id] += idf * frequency * (self.k1 + 1) / denominator
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    if k <= 0:
        raise ValueError("RRF的k必须为正")
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        seen = set()
        for rank, chunk_id in enumerate(ranking, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] += 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class PageSupportingChunk(BaseModel):
    chunk_id: str
    text: str
    similarity: float
    rrf_score: float
    vector_rank: int | None
    keyword_rank: int | None
    section: str
    section_boost: float
    final_score: float


class HybridHit(SearchHit):
    bm25_score: float | None
    rrf_score: float
    vector_rank: int | None
    keyword_rank: int | None
    section: str = ""
    section_boost: float = 0.0
    final_score: float = 0.0
    reranker_score: float | None = None
    primary_financial_evidence: bool = False
    direct_field_evidence: bool = False
    same_page_supporting_chunks: list[PageSupportingChunk] = Field(default_factory=list)


def financial_section_boost(query: str, section: str, amount: float) -> float:
    if amount <= 0 or not any(term in query for term in FINANCIAL_QUERY_TERMS):
        return 0.0
    return amount if any(term in section for term in FINANCIAL_SECTION_TERMS) else 0.0


def deduplicate_page_hits(hits: list[HybridHit]) -> list[HybridHit]:
    """保留每个物理页的最高排名片段，其余同页片段作为补充证据。"""
    grouped: dict[tuple[str, int, int], list[HybridHit]] = {}
    order: list[tuple[str, int, int]] = []
    for hit in hits:
        key = (hit.document_id, hit.page_start, hit.page_end)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(hit)
    result = []
    for key in order:
        representative, *supporting = grouped[key]
        result.append(
            representative.model_copy(
                update={
                    "direct_field_evidence": any(
                        hit.direct_field_evidence for hit in grouped[key]
                    ),
                    "primary_financial_evidence": any(
                        hit.primary_financial_evidence for hit in grouped[key]
                    ),
                    "same_page_supporting_chunks": [
                        PageSupportingChunk(
                            chunk_id=hit.chunk_id,
                            text=hit.text,
                            similarity=hit.similarity,
                            rrf_score=hit.rrf_score,
                            vector_rank=hit.vector_rank,
                            keyword_rank=hit.keyword_rank,
                            section=hit.section,
                            section_boost=hit.section_boost,
                            final_score=hit.final_score,
                        )
                        for hit in supporting
                    ]
                }
            )
        )
    return result


class HybridRetriever:
    def __init__(
        self,
        index: ChromaIndex,
        project_root: Path,
        config: HybridConfig | None = None,
    ) -> None:
        self.index = index
        self.config = config or HybridConfig()
        source = project_root / "data/processed/embedding_chunks.jsonl"
        vector_path = project_root / "data/embeddings/vectors.npz"
        if file_sha256(source) != index.manifest["source_sha256"]:
            raise ValueError("BM25源片段与Chroma版本不一致")
        if file_sha256(vector_path) != index.manifest["vectors_sha256"]:
            raise ValueError("BM25候选对应的向量与Chroma版本不一致")
        records = [
            EmbeddingChunk.model_validate_json(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.chunks = {record.chunk_id: record for record in records}
        with np.load(vector_path, allow_pickle=False) as archive:
            self.vectors = archive["vectors"]
            ids = archive["chunk_ids"].tolist()
        if ids != [record.chunk_id for record in records] or len(self.chunks) != len(records):
            raise ValueError("BM25片段和向量ID不一致")
        self.positions = {chunk_id: i for i, chunk_id in enumerate(ids)}
        # 用正文建关键词索引，减少重复公司前缀和复制表头带来的词频偏置。
        self.bm25 = BM25Index(
            {record.chunk_id: record.text for record in records},
            self.config.bm25_k1,
            self.config.bm25_b,
        )
        self.financial_summary_pages = {
            (record.document_id, record.page_start)
            for record in records
            if record.document_type == "annual_report"
            and is_financial_summary_heading(record.text)
        }

    def _primary_financial_ids(
        self, allowed: set[str], metric_terms: tuple[str, ...]
    ) -> set[str]:
        return {
            chunk_id
            for chunk_id in allowed
            for chunk in [self.chunks[chunk_id]]
            if (chunk.document_id, chunk.page_start) in self.financial_summary_pages
            and "分季度" not in chunk.section
            and any(term in re.sub(r"\s+", "", chunk.text) for term in metric_terms)
        }

    def _one_company(
        self,
        query: str,
        keyword_query: str,
        vector: np.ndarray,
        stock_codes: list[str] | None,
        report_period: str | None,
        document_type: str | None,
        metric_terms: tuple[str, ...] = (),
        field_evidence_kind: str | None = None,
    ) -> list[HybridHit]:
        vector_hits = self.index.query_vector(
            vector,
            self.config.candidate_k,
            stock_codes,
            report_period,
            document_type,
        )
        allowed = {
            chunk_id
            for chunk_id, chunk in self.chunks.items()
            if (not stock_codes or chunk.stock_code in stock_codes)
            and (not report_period or chunk.report_period == report_period)
            and (not document_type or chunk.document_type == document_type)
        }
        lexical = self.bm25.search(keyword_query, allowed, self.config.candidate_k)
        primary_ids = self._primary_financial_ids(allowed, metric_terms) if metric_terms else set()
        primary = (
            self.bm25.search(keyword_query, primary_ids, self.config.candidate_k)
            if primary_ids else []
        )
        direct_ids = {
            chunk_id for chunk_id in allowed
            if field_evidence_kind and is_direct_field_evidence(
                self.chunks[chunk_id].text, self.chunks[chunk_id].section,
                field_evidence_kind, self.chunks[chunk_id].document_type,
            )
        }
        if field_evidence_kind in {"record_date", "payment_date"}:
            # 日期表头与数据行可能分片；仅在同页、同章节合并核查，不跨表继承。
            sections: dict[tuple[str, int, str], list[str]] = defaultdict(list)
            for chunk_id in allowed:
                chunk = self.chunks[chunk_id]
                if chunk.document_type == "announcement" and "相关日期" in chunk.section:
                    sections[(chunk.document_id, chunk.page_start, chunk.section)].append(chunk_id)
            for ids in sections.values():
                ids.sort(key=lambda chunk_id: self.chunks[chunk_id].start_char)
                text = "\n".join(self.chunks[chunk_id].text for chunk_id in ids)
                if is_direct_field_evidence(text, "相关日期", field_evidence_kind, "announcement"):
                    direct_ids.update(ids)
        direct_ranking = self.bm25.search(keyword_query, direct_ids, self.config.candidate_k)
        direct_order = [chunk_id for chunk_id, _ in direct_ranking]
        direct_order.extend(sorted(direct_ids - set(direct_order)))
        lexical_scores = dict(lexical)
        vector_ranks = {hit.chunk_id: rank for rank, hit in enumerate(vector_hits, start=1)}
        keyword_ranks = {chunk_id: rank for rank, (chunk_id, _) in enumerate(lexical, start=1)}
        fused = rrf_fuse(
            [
                [hit.chunk_id for hit in vector_hits],
                [chunk_id for chunk_id, _ in lexical],
                *([[chunk_id for chunk_id, _ in primary]] if primary else []),
                *([direct_order] if direct_order else []),
            ],
            self.config.rrf_k,
        )
        vector = np.asarray(vector, dtype=np.float32)
        vector = vector / np.linalg.norm(vector)
        hits = []
        for chunk_id, score in fused:
            chunk = self.chunks[chunk_id]
            similarity = float(self.vectors[self.positions[chunk_id]] @ vector)
            section_boost = financial_section_boost(
                query, chunk.section, self.config.section_boost
            )
            hits.append(
                HybridHit(
                    chunk_id=chunk_id,
                    parent_chunk_id=chunk.parent_chunk_id,
                    text=chunk.embedding_text,
                    stock_code=chunk.stock_code,
                    stock_name=chunk.stock_name,
                    report_period=chunk.report_period,
                    document_id=chunk.document_id,
                    document_type=chunk.document_type,
                    title=chunk.title,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    source_url=chunk.source_url,
                    similarity=similarity,
                    distance=1 - similarity,
                    header_page=chunk.repeated_header.page_number
                    if chunk.repeated_header
                    else None,
                    table_layout_review_required=chunk.table_layout_review_required,
                    bm25_score=lexical_scores.get(chunk_id),
                    rrf_score=score,
                    vector_rank=vector_ranks.get(chunk_id),
                    keyword_rank=keyword_ranks.get(chunk_id),
                    section=chunk.section,
                    section_boost=section_boost,
                    final_score=score + section_boost,
                    primary_financial_evidence=chunk_id in primary_ids,
                    direct_field_evidence=chunk_id in direct_ids,
                )
            )
        return sorted(hits, key=lambda hit: (
            -hit.direct_field_evidence, -hit.final_score, hit.chunk_id
        ))

    def retrieve(
        self,
        query: str,
        vector: np.ndarray,
        top_k: int = 5,
        stock_codes: list[str] | None = None,
        report_period: str | None = None,
        document_type: str | None = None,
        keyword_query: str | None = None,
        balanced_candidates: bool = False,
        metric_terms: tuple[str, ...] = (),
        field_evidence_kind: str | None = None,
    ) -> list[HybridHit]:
        if not 1 <= top_k <= self.config.candidate_k:
            raise ValueError("top_k必须为正且不超过candidate_k")
        codes = list(dict.fromkeys(normalize_ticker(code) for code in (stock_codes or [])))
        lexical_query = keyword_query or query
        if len(codes) <= 1:
            hits = self._one_company(
                query, lexical_query, vector, codes, report_period, document_type, metric_terms,
                field_evidence_kind,
            )
            if self.config.deduplicate_pages:
                hits = deduplicate_page_hits(hits)
            return hits[:top_k]
        if top_k < len(codes):
            raise ValueError("比较题top_k不能小于公司数量")
        batches = [
            self._one_company(
                query, lexical_query, vector, [code], report_period, document_type, metric_terms,
                field_evidence_kind,
            )
            for code in codes
        ]
        if self.config.deduplicate_pages:
            batches = [deduplicate_page_hits(batch) for batch in batches]
        if balanced_candidates:
            return [hit for batch in batches for hit in batch[:top_k]]
        chosen = [batch[0] for batch in batches if batch]
        rest = sorted(
            [hit for batch in batches for hit in batch[1:]],
            key=lambda hit: (-hit.final_score, hit.chunk_id),
        )
        return sorted(
            chosen + rest[: top_k - len(chosen)],
            key=lambda hit: (-hit.final_score, hit.chunk_id),
        )
