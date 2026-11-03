"""默认CLI和后续服务共用的已发布检索配置；算法仍复用已冻结实现。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.retrieval.chroma_store import ChromaIndex, load_query_model, resolve_query
from app.retrieval.hybrid import HybridConfig, HybridRetriever
from app.retrieval.incremental import RETRIEVAL_PROFILE
from app.retrieval.pdf_extract import file_sha256
from app.retrieval.query_expansion import QueryExpander
from app.retrieval.reranker import LocalReranker
from app.retrieval.service import TaskAwareRetriever

VALIDATED_PARAMETERS = {
    "mode": "hybrid", "top_k": 5, "candidate_k": 20, "query_expansion": "both",
    "page_dedup": True, "section_boost": 0.002, "reranker": True, "task_aware": True,
}


def project_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("配置路径超出本项目")
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_hashes(root: Path, hashes: dict[str, str]) -> None:
    for relative, expected in hashes.items():
        if file_sha256(project_path(root, relative)) != expected:
            raise ValueError(f"文件与已验收版本不一致: {relative}")


def check_holdout(report: dict) -> dict[str, bool]:
    s = report["summary"]
    return {
        "answerable_30": s["answerable_count"] == 30,
        "refusal_unscored_5": s["refusal_not_evaluated"] == 5,
        "recall_085": s["mean_evidence_group_recall_at_5"] >= 0.85,
        "all_sources_24": s["all_sources_hit_count"] >= 24,
        "fields_24": s["answer_fields_complete_count"] >= 24,
        "entities_30": s["entity_correct_count"] == 30,
        "companies_20": len(s["company_coverage"]) == 20,
        "no_zero_hit_companies": not s["companies_with_zero_hits"],
        "top5": report["top_k"] == 5,
    }


def validate_profile(root: Path, profile: dict, during_publish: bool = False) -> Path:
    if not during_publish and (root / "data/incremental/pending_publish.json").exists():
        raise RuntimeError("知识库正在发布或等待恢复，本次查询停止")
    artifact = project_path(root, profile["artifact_root"])
    check_hashes(root, profile["artifact_hashes"])
    manifest = read_json(artifact / "data/chroma/index_manifest.json")
    if manifest["collection_name"] != profile["collection_name"]:
        raise ValueError("检索配置与集合版本不同")
    if profile["kind"] == "validated":
        proof_path = project_path(root, profile["proof"]["path"])
        if file_sha256(proof_path) != profile["proof"]["sha256"]:
            raise ValueError("发布验收凭证发生变化")
        proof = read_json(proof_path)
        if (profile["parameters"] != VALIDATED_PARAMETERS
                or profile["parameters"] != proof["parameters"]
                or profile["collection_name"] != proof["collection_name"]
                or profile["release_id"] != proof["release_id"]
                or profile["artifact_hashes"] != proof["artifact_hashes"]
                or not proof["gate_passed"]):
            raise ValueError("发布配置未绑定已验收策略和语料")
        check_hashes(root, proof["runtime_hashes"])
    elif profile["kind"] != "legacy_rollback":
        raise ValueError("未知检索配置类型")
    if profile.get("device") != "cuda":
        raise ValueError("默认检索配置必须明确使用CUDA；CPU仅供显式诊断")
    return artifact


class PublishedRetriever:
    def __init__(self, profile: dict, index, model, retriever, device: str) -> None:
        self.profile, self.index, self.model = profile, index, model
        self.retriever, self.device = retriever, device

    def retrieve_resolved(self, resolved: dict):
        if self.profile["kind"] == "validated":
            return self.retriever.retrieve(resolved, top_k=5)
        hits = self.retriever.retrieve(
            resolved["query"], self.model.encode_query(resolved["query"]), 5,
            resolved["stock_codes"], resolved["report_period"],
        )
        return hits, []

    def retrieve(self, question: str, conversation: list[dict] | None = None):
        return self.retrieve_resolved(resolve_query(question, conversation))

    def metadata(self) -> dict:
        return {
            "release_id": self.profile["release_id"], "profile_kind": self.profile["kind"],
            "collection_name": self.index.manifest["collection_name"],
            "collection_count": self.index.collection.count(),
            "companies": self.profile["companies"], "device": self.device,
            "validated_strategy": self.profile["kind"] == "validated" and self.device == "cuda",
            "parameters": self.profile["parameters"],
            "coverage_notice": self.profile.get("coverage_notice", ""),
        }


def open_published_retriever(
    root: Path, *, profile: dict | None = None, diagnostic_device: str | None = None,
    during_publish: bool = False,
) -> PublishedRetriever:
    root = root.resolve()
    active = profile is None
    profile = profile if profile is not None else read_json(root / RETRIEVAL_PROFILE)
    artifact = validate_profile(root, profile, during_publish=during_publish)
    if active:
        for relative in profile["artifact_hashes"]:
            export = project_path(root, relative).relative_to(artifact).as_posix()
            if file_sha256(root / export) != profile["artifact_hashes"][relative]:
                raise ValueError(f"活动导出与已发布快照不一致: {export}")
    device = diagnostic_device or profile["device"]
    if device not in {"cuda", "cpu"}:
        raise ValueError("设备只允许cuda或显式cpu，默认不自动降级")
    # 两个已有模型读取环境变量选择设备；创建完后恢复调用环境。
    previous = os.environ.get("RAG_DEVICE")
    os.environ["RAG_DEVICE"] = device
    try:
        import torch

        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("默认检索要求GPU；CUDA不可用，请修复环境或显式选择CPU诊断")
        index = ChromaIndex(root / "data/chroma", artifact_root=artifact)
        model = load_query_model(root, index)
        if profile["kind"] == "validated":
            p = profile["parameters"]
            hybrid = HybridRetriever(index, artifact, HybridConfig(
                candidate_k=p["candidate_k"], deduplicate_pages=p["page_dedup"],
                section_boost=p["section_boost"],
            ))
            reranker = LocalReranker(root)
            if str(reranker.device).split(":")[0] != device:
                raise RuntimeError("重排设备与显式选择不一致")
            retriever = TaskAwareRetriever(
                hybrid, model, reranker, QueryExpander(root / "config/query_expansions.json"),
                expansion_mode=p["query_expansion"],
            )
        else:
            retriever = HybridRetriever(index, artifact)
        if str(model.device).split(":")[0] != device:
            raise RuntimeError("向量模型设备与显式选择不一致")
        return PublishedRetriever(profile, index, model, retriever, device)
    finally:
        if previous is None:
            os.environ.pop("RAG_DEVICE", None)
        else:
            os.environ["RAG_DEVICE"] = previous
