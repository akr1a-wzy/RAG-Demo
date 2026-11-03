"""固定问题、实体解析、向量和TopK，比较纯向量与混合召回。"""

import json
import os
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def measure(case, resolved, hits, elapsed_ms):
    expected = {(source["document_id"], source["page"]) for source in case["expected_sources"]}
    actual = {(hit.document_id, hit.page_start) for hit in hits}
    matched = expected & actual
    return {
        "id": case["id"],
        "question": case["question"],
        "resolved": resolved,
        "all_source_pages_hit": matched == expected,
        "source_page_recall_at_5": len(matched) / len(expected),
        "retrieval_ms": elapsed_ms,
        "missing_sources": [
            {"document_id": doc, "page": page} for doc, page in sorted(expected - actual)
        ],
        "hits": [
            {
                "document_id": hit.document_id,
                "page": hit.page_start,
                "stock_code": hit.stock_code,
                "chunk_id": hit.chunk_id,
                "similarity": hit.similarity,
                **(
                    {
                        "rrf_score": hit.rrf_score,
                        "bm25_score": hit.bm25_score,
                        "vector_rank": hit.vector_rank,
                        "keyword_rank": hit.keyword_rank,
                    }
                    if hasattr(hit, "rrf_score")
                    else {}
                ),
            }
            for hit in hits
        ],
    }


def summarize(results):
    import numpy as np

    measured = [result for result in results if "all_source_pages_hit" in result]
    return {
        "answerable_count": len(measured),
        "all_sources_hit_count": sum(result["all_source_pages_hit"] for result in measured),
        "mean_source_page_recall_at_5": float(
            np.mean([result["source_page_recall_at_5"] for result in measured])
        ),
        "retrieval_mean_ms": float(np.mean([result["retrieval_ms"] for result in measured])),
        "retrieval_p95_ms": float(
            np.percentile([result["retrieval_ms"] for result in measured], 95)
        ),
        "refusal_not_evaluated": len(results) - len(measured),
    }


def main() -> None:
    from app.retrieval.chroma_store import ChromaIndex, load_query_model, resolve_query
    from app.retrieval.hybrid import HYBRID_VERSION, HybridRetriever

    index = ChromaIndex(ROOT / "data/chroma")
    start = perf_counter()
    hybrid = HybridRetriever(index, ROOT)
    build_ms = (perf_counter() - start) * 1000
    model = load_query_model(ROOT, index)
    cases = [
        json.loads(line)
        for line in (ROOT / "eval/dataset.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    vector_cases, hybrid_cases, changes = [], [], []
    embedding_ms = []
    for case in cases:
        if not case["answerable"]:
            entry = {"id": case["id"], "status": "refusal_not_evaluated"}
            vector_cases.append(entry)
            hybrid_cases.append(entry.copy())
            continue
        resolved = resolve_query(case["question"], case["conversation"])
        start = perf_counter()
        vector = model.encode_query(resolved["query"])
        embedding_ms.append((perf_counter() - start) * 1000)
        outputs = {}
        # 交替先运行两路，减少固定先后顺序造成的热缓存偏置。
        modes = ["vector", "hybrid"] if len(embedding_ms) % 2 else ["hybrid", "vector"]
        for mode in modes:
            start = perf_counter()
            if mode == "vector":
                hits = index.retrieve(vector, 5, resolved["stock_codes"], resolved["report_period"])
            else:
                hits = hybrid.retrieve(
                    resolved["query"], vector, 5, resolved["stock_codes"], resolved["report_period"]
                )
            outputs[mode] = measure(case, resolved, hits, (perf_counter() - start) * 1000)
        vector_cases.append(outputs["vector"])
        hybrid_cases.append(outputs["hybrid"])
        difference = (
            outputs["hybrid"]["source_page_recall_at_5"]
            - outputs["vector"]["source_page_recall_at_5"]
        )
        changes.append(
            {
                "id": case["id"],
                "question": case["question"],
                "recall_change": difference,
                "status": "improved"
                if difference > 0
                else "regressed"
                if difference < 0
                else "unchanged",
            }
        )
    vector_summary, hybrid_summary = summarize(vector_cases), summarize(hybrid_cases)
    summary = {
        "vector": vector_summary,
        "hybrid": hybrid_summary,
        "recall_change_percentage_points": 100
        * (
            hybrid_summary["mean_source_page_recall_at_5"]
            - vector_summary["mean_source_page_recall_at_5"]
        ),
        "improved_cases": sum(change["status"] == "improved" for change in changes),
        "regressed_cases": sum(change["status"] == "regressed" for change in changes),
        "keyword_index_build_ms": build_ms,
        "query_embedding_mean_ms": sum(embedding_ms) / len(embedding_ms),
    }
    target = ROOT / "eval/results/hybrid_comparison_report.json"
    report = {
        "summary": summary,
        "retrieval_version": HYBRID_VERSION,
        "config": hybrid.config.__dict__,
        "top_k": 5,
        "index_signature": index.manifest["signature"],
        "changes": changes,
        "vector_cases": vector_cases,
        "hybrid_cases": hybrid_cases,
        "scope": "同一18题开发集的指定来源页评测；不是答案正确率或独立测试集",
        "latency_note": (
            "单机串行样本，检索耗时不含模型加载、问题Embedding和BM25索引初始化；不是线上P95"
        ),
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"对比报告={target}")


if __name__ == "__main__":
    main()
