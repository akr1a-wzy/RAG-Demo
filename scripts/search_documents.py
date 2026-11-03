import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    from app.retrieval.chroma_store import ChromaIndex, load_query_model, resolve_query
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.published import open_published_retriever

    parser = argparse.ArgumentParser(description="查询本地Chroma文档索引，返回证据而非生成答案")
    parser.add_argument("question")
    parser.add_argument("--stock", action="append", help="股票代码；可重复指定")
    parser.add_argument("--period", help="报告期，例如2025")
    parser.add_argument("--type", choices=["annual_report", "announcement"])
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--mode", choices=["hybrid", "vector"])
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--output", type=Path, help="另存完整查询证据")
    args = parser.parse_args()
    if args.top_k is not None and args.top_k < 1:
        parser.error("top-k必须为正数")
    custom = any((args.stock, args.period, args.type, args.top_k is not None, args.mode,
                  args.device != "cuda"))
    if custom and not args.diagnostic:
        parser.error("自定义检索/CPU仅允许--diagnostic；默认入口固定使用已验收配置")
    resolved = resolve_query(args.question)
    codes = args.stock or resolved["stock_codes"]
    period = args.period or resolved["report_period"]
    if not args.diagnostic:
        runtime = open_published_retriever(ROOT)
        hits, tasks = runtime.retrieve_resolved(resolved)
        index = runtime.index
        metadata = runtime.metadata()
    else:
        from app.retrieval.incremental import RETRIEVAL_PROFILE
        from app.retrieval.published import read_json, validate_profile

        profile = read_json(ROOT / RETRIEVAL_PROFILE)
        artifact = validate_profile(ROOT, profile)
        os.environ["RAG_DEVICE"] = args.device
        index = ChromaIndex(ROOT / "data/chroma", artifact_root=artifact)
        model = load_query_model(ROOT, index)
        vector = model.encode_query(resolved["query"])
        if (args.mode or "hybrid") == "hybrid":
            hits = HybridRetriever(index, artifact).retrieve(
                resolved["query"], vector, args.top_k or 5, codes, period, args.type,
            )
        else:
            hits = index.retrieve(vector, args.top_k or 5, codes, period, args.type)
        tasks = []
        metadata = {"diagnostic": True, "validated_strategy": False, "device": model.device,
                    "collection_name": index.manifest["collection_name"],
                    "coverage_notice": "诊断模式，自定义结果不属于已验收默认配置。"}
    report = {
        "question": args.question,
        "query": resolved["query"],
        "stock_codes": codes,
        "report_period": period,
        "collection_count": index.collection.count(),
        "retrieval_mode": args.mode or "hybrid",
        "deployment": metadata,
        "retrieval_tasks": tasks,
        "results": [hit.model_dump() for hit in hits],
        "note": "这是检索证据；相似度不是答案正确率，目前未启用拒答阈值。",
    }
    target = args.output or ROOT / "eval/results/latest_search.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(metadata["coverage_notice"])
    print(f"命中{len(hits)}条；集合共{index.collection.count()}条记录。")
    for rank, hit in enumerate(hits, start=1):
        ranking = f"RRF={hit.rrf_score:.6f}" if hasattr(hit, "rrf_score") else "向量排序"
        print(
            f"{rank}. {hit.title} 第{hit.page_start}页，{ranking}，余弦相似度={hit.similarity:.4f}"
        )
        print(hit.text[:450] + "\n")
    print(f"完整证据={target}")


if __name__ == "__main__":
    main()
