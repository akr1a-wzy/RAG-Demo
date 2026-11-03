"""本机中文Embedding；编码前计数，拒绝静默截断。"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.retrieval.runtime import resolve_torch_device

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class LocalEmbeddingModel:
    def __init__(self, model_path: Path, token_budget: int = 480) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        self.device = resolve_torch_device(torch)
        if self.device == "cpu":
            torch.set_num_threads(4)
        self.model = SentenceTransformer(
            str(model_path.resolve()),
            device=self.device,
            local_files_only=True,
        )
        self.token_budget = min(token_budget, self.model.max_seq_length)
        self.dimension = self.model.get_embedding_dimension()

    def count_tokens(self, text: str) -> int:
        return len(
            self.model.tokenizer(
                text,
                truncation=False,
                add_special_tokens=True,
                verbose=False,
            )["input_ids"]
        )

    def encode(self, texts: list[str], batch_size: int = 16) -> np.ndarray:
        for text in texts:
            tokens = self.count_tokens(text)
            if tokens > self.token_budget:
                raise ValueError(f"Embedding输入超限: {tokens} > {self.token_budget}")
        vectors = self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=True,
        )
        return np.asarray(vectors, dtype=np.float32)

    def encode_query(self, question: str) -> np.ndarray:
        return self.encode([QUERY_INSTRUCTION + question], batch_size=1)[0]


def validate_vectors(vectors: np.ndarray, expected_count: int, dimension: int) -> dict:
    if vectors.shape != (expected_count, dimension):
        raise ValueError(f"向量形状异常: {vectors.shape}，预期={(expected_count, dimension)}")
    if not np.isfinite(vectors).all():
        raise ValueError("向量中存在NaN或Infinity")
    norms = np.linalg.norm(vectors, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-4):
        raise ValueError("向量没有正确归一化")
    return {
        "vector_count": expected_count,
        "dimension": dimension,
        "finite": True,
        "normalized": True,
        "min_norm": float(norms.min()),
        "max_norm": float(norms.max()),
    }
