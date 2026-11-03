"""在同一组非盲测候选上校验CPU/CUDA推理和本机耗时。"""

import argparse
import gc
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")


def main() -> None:
    import torch

    from app.retrieval.embeddings import LocalEmbeddingModel, validate_vectors
    from app.retrieval.hybrid import HybridHit
    from app.retrieval.reranker import LocalReranker, _passage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/retrospective/evidence/gpu-runtime-2026-10-25.json",
    )
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("GPU验证需要CUDA版PyTorch及可用NVIDIA GPU")

    candidate_report = ROOT / "eval/results/ablations_reranker_balanced20_regression.json"
    report = json.loads(candidate_report.read_text(encoding="utf-8"))
    first_case = report["cases"][0]
    chunks_path = (
        ROOT / "data/incremental/runs/9d6087c91f1449c5a149e374c15884fa"
        / "data/processed/embedding_chunks.jsonl"
    )
    chunks = {
        item["chunk_id"]: item
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in [json.loads(line)]
    }
    query = first_case["resolved"]["query"]
    passages = []
    for candidate in first_case["candidate_hits"][:20]:
        chunk = chunks[candidate["chunk_id"]]
        hit = HybridHit(
            **{
                key: chunk[key]
                for key in (
                    "chunk_id", "parent_chunk_id", "stock_code", "stock_name",
                    "report_period", "document_id", "document_type", "title",
                    "page_start", "page_end", "source_url", "table_layout_review_required",
                )
            },
            text=chunk["embedding_text"],
            similarity=0.0,
            distance=1.0,
            header_page=None,
            bm25_score=None,
            rrf_score=0.0,
            vector_rank=None,
            keyword_rank=None,
            section=chunk["section"],
        )
        passages.append(_passage(hit))
    if len(passages) < 5:
        raise ValueError("用于设备对照的非盲测候选不足")

    torch.manual_seed(0)
    tensor = torch.arange(16, device="cuda", dtype=torch.float32).reshape(4, 4)
    product = (tensor @ tensor.T).cpu().numpy()
    np.testing.assert_allclose(product, np.arange(16).reshape(4, 4) @ np.arange(16).reshape(4, 4).T)
    results = {}
    arrays = {}
    original_device = os.environ.get("RAG_DEVICE")
    try:
        for device in ("cpu", "cuda"):
            os.environ["RAG_DEVICE"] = device
            torch.set_num_threads(4)
            embedding = LocalEmbeddingModel(ROOT / "data/models/bge-small-zh-v1.5")
            reranker = LocalReranker(ROOT)
            assert str(next(embedding.model.parameters()).device).startswith(device)
            assert str(next(reranker.model.parameters()).device).startswith(device)
            embedding.encode_query(query)
            reranker.score(query, passages[:1])
            if device == "cuda":
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats()
            start = perf_counter()
            vector = embedding.encode_query(query)
            if device == "cuda":
                torch.cuda.synchronize()
            encoding_seconds = perf_counter() - start
            start = perf_counter()
            scores = reranker.score(query, passages)
            if device == "cuda":
                torch.cuda.synchronize()
            reranking_seconds = perf_counter() - start
            results[device] = {
                "embedding_parameter_device": str(next(embedding.model.parameters()).device),
                "reranker_parameter_device": str(next(reranker.model.parameters()).device),
                "embedding_seconds": encoding_seconds,
                "reranking_seconds": reranking_seconds,
                "reranker_precision": "autocast-float16" if device == "cuda" else "float32",
                "vector_validation": validate_vectors(vector.reshape(1, -1), 1, 512),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated()
                if device == "cuda" else None,
            }
            arrays[device] = (vector, np.asarray(scores))
            del embedding, reranker
            gc.collect()
            torch.cuda.empty_cache()
    finally:
        if original_device is None:
            os.environ.pop("RAG_DEVICE", None)
        else:
            os.environ["RAG_DEVICE"] = original_device

    cpu_vector, cpu_scores = arrays["cpu"]
    gpu_vector, gpu_scores = arrays["cuda"]
    cpu_top5 = np.argsort(-cpu_scores)[:5].tolist()
    gpu_top5 = np.argsort(-gpu_scores)[:5].tolist()
    evidence = {
        "verified_at_utc": datetime.now(UTC).isoformat(),
        "torch_version": torch.__version__,
        "cuda_build": torch.version.cuda,
        "gpu_name": torch.cuda.get_device_name(0),
        "gpu_total_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
        "cuda_tensor_matmul_passed": True,
        "sample": {
            "source": str(candidate_report.relative_to(ROOT)),
            "query_count": 1,
            "candidate_count": len(passages),
            "reranker_batch_size": 8,
            "cpu_threads": 4,
        },
        "devices": results,
        "comparison": {
            "embedding_cosine": float(cpu_vector @ gpu_vector),
            "embedding_max_abs_difference": float(np.max(np.abs(cpu_vector - gpu_vector))),
            "reranker_max_abs_score_difference": float(np.max(np.abs(cpu_scores - gpu_scores))),
            "top5_overlap": len(set(cpu_top5) & set(gpu_top5)),
            "top5_order_identical": cpu_top5 == gpu_top5,
            "reranking_speedup": results["cpu"]["reranking_seconds"]
            / results["cuda"]["reranking_seconds"],
        },
        "timing_scope": "One warmed local query and 20 fixed passages; model loading, "
        "BM25/Chroma retrieval and evaluation scoring excluded. This is not an online benchmark.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
