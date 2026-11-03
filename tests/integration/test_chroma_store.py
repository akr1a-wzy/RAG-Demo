import hashlib
import json

import numpy as np
import pytest

from app.retrieval.chroma_store import ChromaIndex, ingest_vectors
from app.retrieval.embedding_prepare import EmbeddingChunk
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.pdf_extract import file_sha256


@pytest.fixture
def indexed_project(tmp_path):
    source = tmp_path / "data/processed/embedding_chunks.jsonl"
    vector_path = tmp_path / "data/embeddings/vectors.npz"
    source.parent.mkdir(parents=True)
    vector_path.parent.mkdir(parents=True)
    chunks = []
    for index, (code, name) in enumerate([("600519", "贵州茅台"), ("000858", "五粮液")]):
        text = f"{name}营业收入"
        chunks.append(
            EmbeddingChunk(
                chunk_id=str(index),
                parent_chunk_id=f"parent{index}",
                document_id=f"{code}-annual",
                stock_code=code,
                stock_name=name,
                document_type="annual_report",
                title=f"{name}年报",
                report_period="2025",
                publish_date="2026-04-17",
                source_url="https://example.org/report.pdf",
                local_path="data/raw/report.pdf",
                section="财务",
                section_paths=["财务"],
                page_start=7,
                page_end=7,
                start_char=0,
                end_char=len(text),
                text=text,
                character_count=len(text),
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                chunk_type="text",
                oversized=False,
                chunking_version="test",
                chunk_size=800,
                chunk_overlap=100,
                parent_was_oversized=False,
                preparation_version="test",
                embedding_text=text,
                embedding_token_count=10,
                token_budget=480,
                embedding_model="test-model",
                embedding_revision="rev1",
                repeated_header=None,
                table_layout_review_required=False,
            )
        )
    source.write_text(
        "\n".join(chunk.model_dump_json() for chunk in chunks) + "\n", encoding="utf-8"
    )
    np.savez_compressed(
        vector_path, vectors=np.eye(2, dtype=np.float32), chunk_ids=np.asarray(["0", "1"])
    )
    manifest = {
        "source": "data/processed/embedding_chunks.jsonl",
        "vectors": "data/embeddings/vectors.npz",
        "source_sha256": file_sha256(source),
        "vectors_sha256": file_sha256(vector_path),
        "vector_count": 2,
        "dimension": 2,
        "signature": "test-signature",
        "model_id": "test-model",
        "revision": "rev1",
    }
    (vector_path.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ingest_vectors(tmp_path, tmp_path / "data/chroma")
    return tmp_path


def test_ingest_repeats_without_duplicates_and_reopens(indexed_project) -> None:
    root = indexed_project
    repeated = ingest_vectors(root, root / "data/chroma")
    assert repeated["count_before_upsert"] == repeated["count"] == 2
    reopened = ChromaIndex(root / "data/chroma")
    hits = reopened.query_vector(np.array([1.0, 0.0]), 2)
    assert hits[0].chunk_id == "0"
    assert hits[0].similarity == pytest.approx(1.0)
    assert hits[0].page_start == 7


def test_filters_and_comparison_respect_company_boundaries(indexed_project) -> None:
    index = ChromaIndex(indexed_project / "data/chroma")
    query = np.array([1.0, 0.0])
    hits = index.query_vector(query, 2, ["000858"], "2025", "annual_report")
    assert [hit.stock_code for hit in hits] == ["000858"]
    assert index.query_vector(query, 2, ["000858"], "2024") == []
    assert index.query_vector(query, 2, ["000001"]) == []
    balanced = index.retrieve(query, 2, ["600519", "000858"])
    assert {hit.stock_code for hit in balanced} == {"600519", "000858"}
    with pytest.raises(ValueError, match="top_k"):
        index.retrieve(query, 1, ["600519", "000858"])


def test_invalid_vectors_are_rejected(indexed_project) -> None:
    index = ChromaIndex(indexed_project / "data/chroma")
    with pytest.raises(ValueError, match="维度"):
        index.query_vector(np.array([1.0, 0.0, 0.0]))
    with pytest.raises(ValueError, match="数值"):
        index.query_vector(np.array([np.nan, 1.0]))
    with pytest.raises(ValueError, match="零"):
        index.query_vector(np.zeros(2))


def test_source_change_does_not_modify_index(indexed_project) -> None:
    root = indexed_project
    source = root / "data/processed/embedding_chunks.jsonl"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="版本不一致"):
        ingest_vectors(root, root / "data/chroma")
    assert ChromaIndex(root / "data/chroma").collection.count() == 2


def test_hybrid_filters_and_cross_company_quota(indexed_project) -> None:
    root = indexed_project
    index = ChromaIndex(root / "data/chroma")
    hybrid = HybridRetriever(index, root)
    vector = np.array([1.0, 0.0], dtype=np.float32)
    original = vector.copy()
    hits = hybrid.retrieve("营业收入", vector, 2, ["000858"], "2025")
    assert [hit.stock_code for hit in hits] == ["000858"]
    assert hits[0].bm25_score > 0
    assert hits[0].rrf_score > 0
    assert hybrid.retrieve("营业收入", vector, 2, ["000858"], "2024") == []
    compared = hybrid.retrieve("营业收入", vector, 2, ["600519", "000858"])
    assert {hit.stock_code for hit in compared} == {"600519", "000858"}
    assert np.array_equal(vector, original)


def test_hybrid_rejects_stale_source(indexed_project) -> None:
    root = indexed_project
    index = ChromaIndex(root / "data/chroma")
    source = root / "data/processed/embedding_chunks.jsonl"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="版本不一致"):
        HybridRetriever(index, root)
