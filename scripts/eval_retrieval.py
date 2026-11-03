import argparse
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def _source_key(source: dict) -> tuple[str, int]:
    return source["document_id"], source["page"]


def _evidence_groups(case: dict) -> list[dict]:
    if case.get("expected_evidence_groups"):
        groups = []
        for group in case["expected_evidence_groups"]:
            sources = group.get("allowed_sources") or [group["preferred_source"]]
            groups.append(
                {
                    "group_id": group["group_id"],
                    "stock_code": group.get("stock_code"),
                    "sources": {_source_key(source) for source in sources},
                }
            )
        return groups
    return [
        {
            "group_id": f"legacy-source-{number}",
            "stock_code": source["document_id"].split("-", 1)[0],
            "sources": {_source_key(source)},
        }
        for number, source in enumerate(case["expected_sources"], start=1)
    ]


def main() -> None:
    import torch

    from app.retrieval.chroma_store import ChromaIndex, load_query_model, resolve_query
    from app.retrieval.evaluation import evaluate_answer_fields
    from app.retrieval.hybrid import HybridConfig, HybridRetriever
    from app.retrieval.query_expansion import QueryExpander
    from app.retrieval.reranker import LocalReranker, rerank_hits
    from app.retrieval.service import TaskAwareRetriever

    parser = argparse.ArgumentParser(description="评测向量或混合检索的指定来源页召回")
    parser.add_argument("--mode", choices=["hybrid", "vector"], default="hybrid")
    parser.add_argument("--dataset", type=Path, default=ROOT / "eval/dataset.jsonl")
    parser.add_argument("--artifact-root", type=Path, default=ROOT)
    parser.add_argument("--split", help="只评测数据集中的指定split")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--query-expansion",
        choices=["none", "bm25", "vector", "both"],
        default="none",
    )
    parser.add_argument("--page-dedup", action="store_true")
    parser.add_argument("--section-boost", type=float, default=0.0)
    parser.add_argument("--reranker", action="store_true")
    parser.add_argument("--task-aware", action="store_true")
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--case-id", action="append", help="只诊断指定题目ID；可重复")
    args = parser.parse_args()
    if args.candidate_k < 5:
        raise ValueError("candidate_k不能小于最终Top5")
    if args.page_dedup and args.mode != "hybrid":
        raise ValueError("页面去重消融当前只支持hybrid模式")
    if args.task_aware and not (args.reranker and args.page_dedup and args.mode == "hybrid"):
        raise ValueError("按任务检索需要hybrid、reranker和页面去重")

    artifact_root = args.artifact_root.resolve()
    dataset_path = args.dataset.resolve()
    setup_start = perf_counter()
    index = ChromaIndex(ROOT / "data/chroma", artifact_root=artifact_root)
    model = load_query_model(ROOT, index)
    hybrid = (
        HybridRetriever(
            index,
            artifact_root,
            HybridConfig(
                candidate_k=args.candidate_k,
                deduplicate_pages=args.page_dedup,
                section_boost=args.section_boost,
            ),
        )
        if args.mode == "hybrid"
        else None
    )
    expander = (
        QueryExpander(ROOT / "config/query_expansions.json")
        if args.query_expansion != "none"
        else None
    )
    reranker = LocalReranker(ROOT) if args.reranker else None
    task_retriever = (
        TaskAwareRetriever(hybrid, model, reranker, expander, args.query_expansion)
        if args.task_aware else None
    )
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.split:
        cases = [case for case in cases if case.get("split") == args.split]
    if args.case_id:
        requested = set(args.case_id)
        cases = [case for case in cases if case.get("id") in requested]
        missing = requested - {case["id"] for case in cases}
        if missing:
            raise ValueError(f"指定题目不存在或不属于当前split: {sorted(missing)}")
    if not cases:
        raise ValueError("评测集为空")

    setup_seconds = perf_counter() - setup_start
    evaluation_start = perf_counter()
    results = []
    for case in cases:
        if not case["answerable"]:
            results.append({"id": case["id"], "status": "refusal_not_evaluated"})
            continue
        query_start = perf_counter()
        resolved = resolve_query(case["question"], case["conversation"])
        expansion = expander.expand(resolved["query"]) if expander else None
        expanded_query = expansion.expanded_query if expansion else resolved["query"]
        vector_query = (
            expanded_query
            if args.query_expansion in ("vector", "both")
            else resolved["query"]
        )
        keyword_query = (
            expanded_query
            if args.query_expansion in ("bm25", "both")
            else resolved["query"]
        )
        candidate_hits = []
        task_traces = []
        if task_retriever:
            hits, task_traces = task_retriever.retrieve(resolved)
        elif hybrid is not None:
            vector = model.encode_query(vector_query)
            hits = hybrid.retrieve(
                resolved["query"],
                vector,
                args.candidate_k if reranker else 5,
                resolved["stock_codes"],
                resolved["report_period"],
                keyword_query=keyword_query,
                balanced_candidates=bool(reranker),
            )
            candidate_hits = list(hits)
            if reranker:
                hits = rerank_hits(
                    resolved["query"],
                    hits,
                    reranker,
                    5,
                    resolved["stock_codes"],
                )
        else:
            vector = model.encode_query(vector_query)
            hits = index.retrieve(vector, 5, resolved["stock_codes"], resolved["report_period"])

        query_seconds = perf_counter() - query_start
        actual = {(hit.document_id, hit.page_start) for hit in hits}
        groups = _evidence_groups(case)
        group_results = []
        for group in groups:
            matched = sorted(group["sources"] & actual)
            group_results.append(
                {
                    "group_id": group["group_id"],
                    "stock_code": group["stock_code"],
                    "matched": bool(matched),
                    "matched_sources": [
                        {"document_id": document_id, "page": page}
                        for document_id, page in matched
                    ],
                }
            )
        matched_groups = [group for group in group_results if group["matched"]]
        matched_stock_codes = sorted(
            {group["stock_code"] for group in matched_groups if group["stock_code"]}
        )

        field_results = evaluate_answer_fields(case, hits, groups)
        matched_fields = sum(field["matched"] for field in field_results)
        field_recall = matched_fields / len(field_results) if field_results else 0.0
        group_recall = len(matched_groups) / len(groups)
        results.append(
            {
                "id": case["id"],
                "query_seconds": query_seconds,
                "question": case["question"],
                "resolved": resolved,
                "retrieval_tasks": task_traces,
                "query_expansion": {
                    "mode": args.query_expansion,
                    "vector_query": vector_query,
                    "keyword_query": keyword_query,
                    "matched_rule_ids": expansion.matched_rule_ids if expansion else [],
                    "added_terms": expansion.added_terms if expansion else [],
                },
                "all_source_pages_hit": len(matched_groups) == len(groups),
                "source_page_recall_at_5": group_recall,
                "evidence_group_recall_at_5": group_recall,
                "evidence_groups": group_results,
                "answer_field_recall_at_5": field_recall,
                "answer_fields_complete": bool(field_results)
                and matched_fields == len(field_results),
                "answer_fields": field_results,
                "entity_correct": set(resolved["stock_codes"])
                == set(case["expected_stock_codes"]),
                "matched_stock_codes": matched_stock_codes,
                "missing_evidence_groups": [
                    group["group_id"] for group in group_results if not group["matched"]
                ],
                "hits": [
                    {
                        "document_id": hit.document_id,
                        "page": hit.page_start,
                        "stock_code": hit.stock_code,
                        "similarity": hit.similarity,
                        "chunk_id": hit.chunk_id,
                        "same_page_supporting_chunk_ids": [
                            item.chunk_id
                            for item in getattr(hit, "same_page_supporting_chunks", [])
                        ],
                        "section": getattr(hit, "section", ""),
                        "section_boost": getattr(hit, "section_boost", 0.0),
                        "final_score": getattr(hit, "final_score", None),
                        "reranker_score": getattr(hit, "reranker_score", None),
                        "primary_financial_evidence": getattr(
                            hit, "primary_financial_evidence", False
                        ),
                        "direct_field_evidence": getattr(hit, "direct_field_evidence", False),
                    }
                    for hit in hits
                ],
                "candidate_hits": [
                    {
                        "document_id": hit.document_id,
                        "page": hit.page_start,
                        "stock_code": hit.stock_code,
                        "chunk_id": hit.chunk_id,
                        "section": getattr(hit, "section", ""),
                        "first_stage_score": getattr(hit, "final_score", None),
                    }
                    for hit in candidate_hits
                ]
                if reranker
                else [],
            }
        )

    answerable = [result for result in results if "all_source_pages_hit" in result]
    expected_codes = sorted(
        {code for case in cases if case["answerable"] for code in case["expected_stock_codes"]}
    )
    company_coverage = {
        code: {
            "case_count": sum(
                code in case["expected_stock_codes"] for case in cases if case["answerable"]
            ),
            "matched_case_count": sum(
                code in result.get("matched_stock_codes", []) for result in answerable
            ),
        }
        for code in expected_codes
    }
    summary = {
        "answerable_count": len(answerable),
        "all_sources_hit_count": sum(result["all_source_pages_hit"] for result in answerable),
        "mean_source_page_recall_at_5": sum(
            result["source_page_recall_at_5"] for result in answerable
        )
        / len(answerable),
        "mean_evidence_group_recall_at_5": sum(
            result["evidence_group_recall_at_5"] for result in answerable
        )
        / len(answerable),
        "answer_fields_complete_count": sum(
            result["answer_fields_complete"] for result in answerable
        ),
        "mean_answer_field_recall_at_5": sum(
            result["answer_field_recall_at_5"] for result in answerable
        )
        / len(answerable),
        "entity_correct_count": sum(result["entity_correct"] for result in answerable),
        "refusal_not_evaluated": len(results) - len(answerable),
        "company_coverage": company_coverage,
        "companies_with_zero_hits": [
            code for code, values in company_coverage.items() if values["matched_case_count"] == 0
        ],
        "metric_note": (
            "按证据组评测；旧数据集每个指定物理页等同一个证据组。"
            "页码或字段命中都不等于生成答案正确。"
        ),
    }
    report = {
        "summary": summary,
        "collection_name": index.manifest["collection_name"],
        "top_k": 5,
        "retrieval_mode": args.mode,
        "page_dedup": args.page_dedup,
        "query_expansion": args.query_expansion,
        "section_boost": args.section_boost,
        "reranker": args.reranker,
        "task_aware": args.task_aware,
        "pipeline_version": "task-aware-direct-field-v4"
        if args.task_aware else "legacy-hybrid-v1",
        "answer_field_metric_version": "lexical-values-with-boundaries-and-group-scope-v2",
        "candidate_k": args.candidate_k,
        "embedding_device": model.device,
        "reranker_device": reranker.device if reranker else None,
        "runtime": {
            "torch_version": torch.__version__,
            "cuda_build": torch.version.cuda,
            "cuda_available": torch.cuda.is_available(),
            "gpu_name": torch.cuda.get_device_name(0) if model.device == "cuda" else None,
            "reranker_precision": "autocast-float16"
            if reranker and reranker.device == "cuda"
            else "float32",
        },
        "timing": {
            "setup_seconds": setup_seconds,
            "evaluation_seconds": perf_counter() - evaluation_start,
            "mean_query_seconds": sum(item["query_seconds"] for item in answerable)
            / len(answerable),
            "scope": "single-process local evaluation; query timings include query encoding, "
            "retrieval and optional reranking; setup and gold scoring excluded; no warmup",
        },
        "reranker_model": reranker.manifest if reranker else None,
        "query_expansion_config": str(expander.path) if expander else None,
        "query_expansion_config_sha256": expander.sha256 if expander else None,
        "dataset": str(dataset_path),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "split": args.split,
        "cases": results,
    }
    filename = "hybrid_retrieval_report.json" if args.mode == "hybrid" else "retrieval_report.json"
    target = args.output.resolve() if args.output else ROOT / "eval/results" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告={target}")


if __name__ == "__main__":
    main()
