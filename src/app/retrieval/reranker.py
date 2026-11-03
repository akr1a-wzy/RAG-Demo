"""本地Cross-Encoder候选重排，不改变第一阶段召回集合。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from app.retrieval.hybrid import HybridHit
from app.retrieval.runtime import resolve_torch_device


class LocalReranker:
    def __init__(self, root: Path) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        manifest_path = root / "data/models/reranker_model.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        model_path = root / self.manifest["local_path"]
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_path, local_files_only=True
        )
        self.device = resolve_torch_device(torch)
        self.model.to(self.device)
        self.model.eval()
        self.torch = torch

    def score(self, query: str, passages: Sequence[str], batch_size: int = 8) -> list[float]:
        scores = []
        for start in range(0, len(passages), batch_size):
            batch = passages[start : start + batch_size]
            inputs = self.tokenizer(
                [[query, passage] for passage in batch],
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with self.torch.no_grad():
                if self.device == "cuda":
                    with self.torch.autocast(device_type="cuda", dtype=self.torch.float16):
                        logits = self.model(**inputs, return_dict=True).logits.view(-1)
                else:
                    logits = self.model(**inputs, return_dict=True).logits.view(-1)
            scores.extend(logits.detach().cpu().float().tolist())
        if len(scores) != len(passages) or not np.isfinite(scores).all():
            raise ValueError("重排分数数量或数值异常")
        return scores


def _passage(hit: HybridHit) -> str:
    supporting = "\n".join(item.text for item in hit.same_page_supporting_chunks)
    return (
        f"公司：{hit.stock_name}；文档：{hit.title}；页码：{hit.page_start}；"
        f"章节：{hit.section}\n{hit.text}\n{supporting}"
    ).strip()


def rerank_hits(
    query: str,
    hits: list[HybridHit],
    reranker: LocalReranker,
    top_k: int,
    stock_codes: list[str] | None = None,
    prefer_primary_financial: bool = False,
    prefer_direct_field: bool = False,
) -> list[HybridHit]:
    if not 1 <= top_k <= len(hits):
        raise ValueError("重排top_k超出候选范围")
    scores = reranker.score(query, [_passage(hit) for hit in hits])
    ranked = sorted(
        zip(hits, scores, strict=True),
        key=lambda item: (
            -(prefer_direct_field and item[0].direct_field_evidence),
            -(prefer_primary_financial and item[0].primary_financial_evidence),
            -item[1],
            item[0].chunk_id,
        ),
    )
    codes = list(dict.fromkeys(stock_codes or []))
    if len(codes) <= 1:
        return [
            hit.model_copy(update={"reranker_score": score}) for hit, score in ranked[:top_k]
        ]
    selected = []
    selected_ids = set()
    for code in codes:
        match = next(((hit, score) for hit, score in ranked if hit.stock_code == code), None)
        if match:
            selected.append(match)
            selected_ids.add(match[0].chunk_id)
    selected.extend(
        (hit, score)
        for hit, score in ranked
        if hit.chunk_id not in selected_ids
    )
    return [
        hit.model_copy(update={"reranker_score": score})
        for hit, score in selected[:top_k]
    ]
