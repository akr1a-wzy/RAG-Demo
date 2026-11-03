"""本地持久化索引：存储预生成向量，不调用Chroma默认Embedding。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from app.retrieval.embedding_prepare import EmbeddingChunk
from app.retrieval.embeddings import validate_vectors
from app.retrieval.pdf_extract import file_sha256
from app.tools.ticker import ALIAS_TO_CODE, CODE_TO_CANONICAL_NAME, normalize_ticker

INDEX_VERSION = "chroma-cosine-v1"


def persistent_client(path: Path):
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(
        path=str(path.resolve()),
        settings=Settings(anonymized_telemetry=False),
    )


class SearchHit(BaseModel):
    chunk_id: str
    parent_chunk_id: str
    text: str
    stock_code: str
    stock_name: str
    report_period: str
    document_id: str
    document_type: str
    title: str
    page_start: int
    page_end: int
    source_url: str
    distance: float
    similarity: float
    header_page: int | None
    table_layout_review_required: bool


def build_filter(
    stock_codes: list[str] | None = None,
    report_period: str | None = None,
    document_type: str | None = None,
) -> dict | None:
    clauses = []
    if stock_codes:
        clauses.append(
            {
                "stock_code": {
                    "$in": list(dict.fromkeys(normalize_ticker(code) for code in stock_codes))
                }
            }
        )
    if report_period:
        clauses.append({"report_period": {"$eq": report_period}})
    if document_type:
        clauses.append({"document_type": {"$eq": document_type}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def resolve_query(question: str, conversation: list[dict] | None = None) -> dict:
    """20家公司的规则基线；当前实体优先，连续追问继承最近用户问题。"""

    def extract(text: str) -> tuple[list[str], str | None]:
        mentions = [
            (text.index(name), -len(name), code)
            for name, code in ALIAS_TO_CODE.items()
            if name in text
        ]
        mentions.extend(
            (match.start(), -len(match.group(0)), match.group(1))
            for match in re.finditer(r"(?<!\d)(?:SH|SZ|BJ)?(\d{6})(?!\d)", text, re.I)
        )
        years = re.findall(r"(20\d{2})\s*年", text)
        codes = list(dict.fromkeys(item[2] for item in sorted(mentions)))
        return codes, years[0] if years else None

    codes, period = extract(question)
    for message in reversed(conversation or []):
        if message.get("role") != "user":
            continue
        old_codes, old_period = extract(message.get("content", ""))
        codes = codes or old_codes
        period = period or old_period
        if codes and period:
            break
    context = "、".join(CODE_TO_CANONICAL_NAME.get(code, code) for code in codes)
    effective_query = f"{context} {period or ''}年 {question}" if context else question
    return {"query": effective_query, "stock_codes": codes, "report_period": period}


def chunk_metadata(chunk: EmbeddingChunk) -> dict:
    return {
        "parent_chunk_id": chunk.parent_chunk_id,
        "stock_code": chunk.stock_code,
        "stock_name": chunk.stock_name,
        "report_period": chunk.report_period,
        "document_id": chunk.document_id,
        "document_type": chunk.document_type,
        "title": chunk.title,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_url": chunk.source_url,
        "publish_date": chunk.publish_date,
        "local_path": chunk.local_path,
        "section": chunk.section,
        "start_char": chunk.start_char,
        "end_char": chunk.end_char,
        "content_hash": chunk.content_hash,
        "embedding_token_count": chunk.embedding_token_count,
        "header_page": chunk.repeated_header.page_number if chunk.repeated_header else 0,
        "table_layout_review_required": chunk.table_layout_review_required,
    }


def ingest_vectors(project_root: Path, chroma_path: Path, *, activate: bool = True) -> dict:
    manifest_path = project_root / "data/embeddings/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = project_root / manifest["source"]
    vector_path = project_root / manifest["vectors"]
    if file_sha256(source) != manifest["source_sha256"]:
        raise ValueError("片段文件与向量版本不一致，请重新生成Embedding")
    if file_sha256(vector_path) != manifest["vectors_sha256"]:
        raise ValueError("向量文件哈希不一致")
    chunks = [
        EmbeddingChunk.model_validate_json(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with np.load(vector_path, allow_pickle=False) as archive:
        vectors, ids = archive["vectors"], archive["chunk_ids"].tolist()
    if ids != [chunk.chunk_id for chunk in chunks] or len(set(ids)) != len(ids):
        raise ValueError("片段ID、向量顺序或唯一性不符合要求")
    if len(chunks) != manifest["vector_count"]:
        raise ValueError("向量清单数量不一致")
    if any(
        chunk.embedding_model != manifest["model_id"]
        or chunk.embedding_revision != manifest["revision"]
        for chunk in chunks
    ):
        raise ValueError("片段与向量的模型版本不一致")
    validate_vectors(vectors, len(chunks), manifest["dimension"])
    signature = hashlib.sha256(f"{manifest['signature']}:{INDEX_VERSION}".encode()).hexdigest()
    name = f"annual_announcements_{signature[:20]}"
    client = persistent_client(chroma_path)
    metadata = {
        "signature": signature,
        "index_version": INDEX_VERSION,
        "model_id": manifest["model_id"],
        "revision": manifest["revision"],
        "dimension": manifest["dimension"],
        "metric": "cosine",
    }
    collection = client.get_or_create_collection(
        name=name,
        embedding_function=None,
        metadata=metadata,
        configuration={"hnsw": {"space": "cosine", "ef_search": 100}},
    )
    if collection.metadata != metadata:
        raise ValueError("现有集合的版本信息不一致")
    before = collection.count()
    for start in range(0, len(chunks), 128):
        batch = chunks[start : start + 128]
        collection.upsert(
            ids=ids[start : start + 128],
            embeddings=vectors[start : start + 128].tolist(),
            documents=[chunk.embedding_text for chunk in batch],
            metadatas=[chunk_metadata(chunk) for chunk in batch],
        )
    if collection.count() != len(chunks):
        raise ValueError("Chroma入库数量不一致")
    # 逐条核对已入库文字、元数据与ID；抽检向量与原始NPZ一致。
    expected = {chunk.chunk_id: chunk for chunk in chunks}
    seen = set()
    for offset in range(0, len(chunks), 256):
        stored = collection.get(limit=256, offset=offset, include=["documents", "metadatas"])
        for chunk_id, text, item in zip(
            stored["ids"],
            stored["documents"],
            stored["metadatas"],
            strict=True,
        ):
            original = expected[chunk_id]
            if text != original.embedding_text or item != chunk_metadata(original):
                raise ValueError(f"存储校验失败: {chunk_id}")
            seen.add(chunk_id)
    if seen != set(ids):
        raise ValueError("存储ID集合不一致")
    samples = collection.get(ids=ids[:16], include=["embeddings"])
    lookup = {chunk_id: vectors[i] for i, chunk_id in enumerate(ids)}
    for chunk_id, vector in zip(samples["ids"], samples["embeddings"], strict=True):
        if not np.allclose(vector, lookup[chunk_id], atol=1e-6):
            raise ValueError("存储向量与输入向量不同")
    index_manifest = {
        **metadata,
        "collection_name": name,
        "count": len(chunks),
        "source_sha256": manifest["source_sha256"],
        "vectors_sha256": manifest["vectors_sha256"],
    }
    if activate:
        temporary = chroma_path / "index_manifest.json.tmp"
        temporary.write_text(
            json.dumps(index_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(chroma_path / "index_manifest.json")
    return {
        **index_manifest,
        "count_before_upsert": before,
        "verified_documents": len(seen),
        "verified_vectors": len(samples["ids"]),
    }


class ChromaIndex:
    def __init__(self, path: Path, artifact_root: Path | None = None) -> None:
        self.artifact_root = (artifact_root or path.parents[1]).resolve()
        manifest_path = self.artifact_root / "data/chroma/index_manifest.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.client = persistent_client(path)
        self.collection = self.client.get_collection(
            self.manifest["collection_name"],
            embedding_function=None,
        )
        metadata = self.collection.metadata or {}
        if metadata.get("signature") != self.manifest["signature"]:
            raise ValueError("索引版本不匹配")
        if self.collection.count() != self.manifest["count"]:
            raise ValueError("索引条数不匹配")

    def query_vector(
        self,
        vector: np.ndarray,
        top_k: int = 5,
        stock_codes: list[str] | None = None,
        report_period: str | None = None,
        document_type: str | None = None,
    ) -> list[SearchHit]:
        if top_k < 1:
            raise ValueError("top_k必须为正整数")
        vector = np.asarray(vector, dtype=np.float32)
        if vector.shape != (self.manifest["dimension"],) or not np.isfinite(vector).all():
            raise ValueError("查询向量维度或数值错误")
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise ValueError("查询向量不能为零")
        vector = vector / norm
        result = self.collection.query(
            query_embeddings=[vector.tolist()],
            n_results=min(top_k, self.manifest["count"]),
            where=build_filter(stock_codes, report_period, document_type),
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for chunk_id, text, metadata, distance in zip(
            result["ids"][0],
            result["documents"][0],
            result["metadatas"][0],
            result["distances"][0],
            strict=True,
        ):
            hits.append(
                SearchHit(
                    chunk_id=chunk_id,
                    text=text,
                    distance=float(distance),
                    similarity=float(1.0 - distance),
                    **{
                        key: metadata[key]
                        for key in (
                            "parent_chunk_id",
                            "stock_code",
                            "stock_name",
                            "report_period",
                            "document_id",
                            "document_type",
                            "title",
                            "page_start",
                            "page_end",
                            "source_url",
                            "table_layout_review_required",
                        )
                    },
                    header_page=metadata["header_page"] or None,
                )
            )
        return hits

    def retrieve(
        self,
        vector: np.ndarray,
        top_k: int = 5,
        stock_codes: list[str] | None = None,
        report_period: str | None = None,
        document_type: str | None = None,
    ) -> list[SearchHit]:
        # 比较题给每家公司至少一个候选名额，避免结果被单个公司占满。
        codes = list(dict.fromkeys(stock_codes or []))
        if len(codes) <= 1:
            return self.query_vector(vector, top_k, codes, report_period, document_type)
        if top_k < len(codes):
            raise ValueError("比较题top_k不能小于公司数量")
        batches = [
            self.query_vector(vector, top_k, [code], report_period, document_type) for code in codes
        ]
        chosen = [batch[0] for batch in batches if batch]
        rest = sorted(
            [hit for batch in batches for hit in batch[1:]],
            key=lambda hit: hit.similarity,
            reverse=True,
        )
        return sorted(
            chosen + rest[: top_k - len(chosen)], key=lambda hit: hit.similarity, reverse=True
        )


def load_query_model(project_root: Path, index: ChromaIndex):
    from app.retrieval.embeddings import LocalEmbeddingModel

    local = json.loads(
        (project_root / "data/models/embedding_model.json").read_text(encoding="utf-8")
    )
    if (
        local["model_id"] != index.manifest["model_id"]
        or local["revision"] != index.manifest["revision"]
    ):
        raise ValueError("查询模型与Chroma索引版本不同，请重建索引或切回匹配模型")
    model = LocalEmbeddingModel(project_root / local["local_path"])
    if model.dimension != index.manifest["dimension"]:
        raise ValueError("查询模型与Chroma向量维度不同")
    return model
