import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    import numpy as np

    from app.retrieval.embedding_prepare import EmbeddingChunk
    from app.retrieval.embeddings import LocalEmbeddingModel, validate_vectors
    from app.retrieval.pdf_extract import file_sha256

    source = ROOT / "data/processed/embedding_chunks.jsonl"
    model_manifest = json.loads(
        (ROOT / "data/models/embedding_model.json").read_text(encoding="utf-8")
    )
    chunks = [
        EmbeddingChunk.model_validate_json(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not chunks:
        raise ValueError("没有待向量化片段")
    if any(
        c.embedding_revision != model_manifest["revision"]
        or c.embedding_model != model_manifest["model_id"]
        for c in chunks
    ):
        raise ValueError("准备片段的模型版本与本地模型不同，请重新处理")
    model_path = ROOT / model_manifest["local_path"]
    model = LocalEmbeddingModel(model_path)
    ids = np.asarray([c.chunk_id for c in chunks])
    source_hash = file_sha256(source)
    weights_hash = file_sha256(model_path / "model.safetensors")
    signature = hashlib.sha256(
        json.dumps(
            {
                "source_hash": source_hash,
                "weights_hash": weights_hash,
                "revision": model_manifest["revision"],
                "normalize": True,
                "dtype": "float32",
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    target_dir = ROOT / "data/embeddings"
    target_dir.mkdir(parents=True, exist_ok=True)
    vector_path = target_dir / "vectors.npz"
    manifest_path = target_dir / "manifest.json"
    cached = False
    if vector_path.exists() and manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        cached = saved.get("signature") == signature and (
            saved.get("vectors_sha256") == file_sha256(vector_path)
        )
    # 即使命中缓存，也重新检查真实token长度，不能依赖旧记录里的计数。
    token_counts = [model.count_tokens(c.embedding_text) for c in chunks]
    if max(token_counts) > model.token_budget:
        raise ValueError(
            f"检测到超限输入: 实际最大={max(token_counts)}，预算={model.token_budget}，"
            f"实际模型上限={model.model.max_seq_length}；请使用同一个分词器重新切分"
        )
    if any(c.embedding_token_count != count for c, count in zip(chunks, token_counts, strict=True)):
        raise ValueError("预处理token计数与实际模型不一致")
    print(f"准备生成 {len(chunks)} 条 {model.dimension} 维向量；缓存命中={cached}", flush=True)
    if cached:
        with np.load(vector_path, allow_pickle=False) as saved_vectors:
            vectors = saved_vectors["vectors"]
            if not np.array_equal(ids, saved_vectors["chunk_ids"]):
                raise ValueError("向量顺序和片段ID不一致")
    else:
        vectors = model.encode([c.embedding_text for c in chunks])
        validate_vectors(vectors, len(chunks), model.dimension)
        temporary = vector_path.with_suffix(".npz.tmp")
        try:
            with temporary.open("wb") as stream:
                np.savez_compressed(stream, vectors=vectors, chunk_ids=ids)
            temporary.replace(vector_path)
        finally:
            temporary.unlink(missing_ok=True)
    validation = validate_vectors(vectors, len(chunks), model.dimension)
    manifest = {
        "model_id": model_manifest["model_id"],
        "revision": model_manifest["revision"],
        "weights_sha256": weights_hash,
        "signature": signature,
        "source": "data/processed/embedding_chunks.jsonl",
        "source_sha256": source_hash,
        "vectors": "data/embeddings/vectors.npz",
        "vectors_sha256": file_sha256(vector_path),
        "normalized": True,
        "dtype": "float32",
        "dimension": model.dimension,
        "model_max_tokens": model.model.max_seq_length,
        "token_budget": model.token_budget,
        "max_actual_tokens": max(token_counts),
        "silent_truncation_count": 0,
        "vector_count": len(chunks),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # 仅验证向量能够用于相似度计算；不是完整召回评测，也不调用LLM。
    examples = []
    for question, stock_code in [
        ("贵州茅台2025年研发投入合计是多少？", "600519"),
        ("五粮液2025年营业收入是多少？", "000858"),
        ("泸州老窖采用哪些销售模式？", "000568"),
    ]:
        query_vector = model.encode_query(question)
        scores = vectors @ query_vector
        candidates = [i for i, c in enumerate(chunks) if c.stock_code == stock_code]
        top = sorted(candidates, key=lambda i: float(scores[i]), reverse=True)[:3]
        examples.append(
            {
                "question": question,
                "top3": [
                    {
                        "chunk_id": chunks[i].chunk_id,
                        "document_id": chunks[i].document_id,
                        "page": chunks[i].page_start,
                        "score": float(scores[i]),
                    }
                    for i in top
                ],
            }
        )
    report = {
        "validation": validation,
        "cache_hit": cached,
        "max_actual_tokens": max(token_counts),
        "silent_truncation_count": 0,
        "smoke_examples": examples,
        "scope": "向量长度、有限值、归一化和相似度冒烟检查；尚未入Chroma或验证问答准确率",
    }
    report_path = ROOT / "eval/results/embedding_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"向量={vector_path}\n版本清单={manifest_path}\n报告={report_path}")


if __name__ == "__main__":
    main()
