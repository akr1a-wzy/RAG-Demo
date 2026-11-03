"""供评测与后续问答服务复用的按实体/证据任务检索层。"""

from __future__ import annotations

from dataclasses import asdict

from app.retrieval.hybrid import HybridHit, HybridRetriever
from app.retrieval.query_expansion import QueryExpander
from app.retrieval.query_tasks import plan_query_tasks
from app.retrieval.reranker import LocalReranker, rerank_hits


class TaskAwareRetriever:
    def __init__(
        self, hybrid: HybridRetriever, model, reranker: LocalReranker,
        expander: QueryExpander | None = None, expansion_mode: str = "both",
    ) -> None:
        self.hybrid = hybrid
        self.model = model
        self.reranker = reranker
        self.expander = expander
        self.expansion_mode = expansion_mode

    def retrieve(self, resolved: dict, top_k: int = 5) -> tuple[list[HybridHit], list[dict]]:
        tasks = plan_query_tasks(
            resolved["query"], resolved["stock_codes"], resolved["report_period"]
        )
        if len(tasks) > top_k:
            raise ValueError("证据任务数量超过TopK覆盖能力，请减少公司或拆分问题")
        batches = []
        traces = []
        for task in tasks:
            expanded = self.expander.expand(task.query) if self.expander else None
            expansion = expanded.expanded_query if expanded else task.query
            vector_query = (
                expansion if self.expansion_mode in ("vector", "both") else task.query
            )
            keyword_query = (
                expansion if self.expansion_mode in ("bm25", "both") else task.query
            )
            codes = [task.stock_code] if task.stock_code else resolved["stock_codes"]
            candidates = self.hybrid.retrieve(
                task.query, self.model.encode_query(vector_query),
                self.hybrid.config.candidate_k, codes, task.report_period,
                keyword_query=keyword_query, balanced_candidates=True,
                metric_terms=task.metric_terms if task.prefer_primary_financial else (),
                field_evidence_kind=task.field_evidence_kind,
            )
            ranked = (
                rerank_hits(
                    task.query, candidates, self.reranker, len(candidates), codes,
                    prefer_primary_financial=task.prefer_primary_financial,
                    prefer_direct_field=task.field_evidence_kind is not None,
                )
                if candidates else []
            )
            batches.append(ranked)
            traces.append({
                **asdict(task),
                "vector_query": vector_query,
                "keyword_query": keyword_query,
                "matched_rule_ids": expanded.matched_rule_ids if expanded else [],
                "candidates": [
                    {
                        "chunk_id": hit.chunk_id, "document_id": hit.document_id,
                        "page": hit.page_start, "stock_code": hit.stock_code,
                        "primary_financial_evidence": hit.primary_financial_evidence,
                        "direct_field_evidence": hit.direct_field_evidence,
                    }
                    for hit in candidates
                ],
            })
        chosen = []
        pages = set()
        # 跨任务分数不可直接比较；逐轮按任务取下一候选，先保障每个任务一个位置。
        queues = [iter(batch) for batch in batches]
        while len(chosen) < top_k:
            added = False
            for queue in queues:
                for hit in queue:
                    key = (hit.document_id, hit.page_start)
                    if key not in pages:
                        pages.add(key)
                        chosen.append(hit)
                        added = True
                        break
                if len(chosen) == top_k:
                    break
            if not added:
                break
        return chosen, traces
