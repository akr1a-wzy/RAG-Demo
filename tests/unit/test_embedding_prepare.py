import hashlib

import numpy as np
import pytest

from app.retrieval.chunking import ChunkConfig, split_page
from app.retrieval.embedding_prepare import prepare_chunk
from app.retrieval.embeddings import LocalEmbeddingModel, validate_vectors
from app.retrieval.pdf_extract import PageRecord


def page(text: str) -> PageRecord:
    return PageRecord(
        page_id="doc:p1",
        document_id="doc",
        stock_code="600519",
        stock_name="茅台",
        document_type="annual_report",
        title="年报",
        report_period="2025",
        publish_date="2026-04-17",
        source_url="https://example.org/report.pdf",
        local_path="data/raw/test.pdf",
        page_number=1,
        text=text,
        character_count=len(text),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def test_table_rows_header_and_parent_coverage() -> None:
    header = "单位：元\n项目 2025年 2024年\n"
    rows = [f"项目{i} 123,456.78 98,765.43\n" for i in range(40)]
    source = page(header + "".join(rows))
    parents, _ = split_page(source, ChunkConfig())
    assert len(parents) == 1 and parents[0].oversized
    children = prepare_chunk(parents[0], {("doc", 1): source}, len, "test", "rev", 300)
    assert len(children) > 1
    assert all(c.embedding_token_count <= 300 for c in children)
    assert all(c.repeated_header.text == header.strip() for c in children)
    assert all(c.parent_chunk_id == parents[0].chunk_id for c in children)
    for row in rows:
        assert any(row.strip() in c.text for c in children)
    assert "".join(c.text for c in children).replace("\n", "") == source.text.replace("\n", "")


def test_prose_recut_does_not_split_financial_number() -> None:
    source = page("公司经营情况。" * 60 + "123,456,789.12" + "经营情况" * 60)
    parents, _ = split_page(source, ChunkConfig())
    children = prepare_chunk(parents[0], {("doc", 1): source}, len, "test", "rev", 180)
    assert any("123,456,789.12" in child.text for child in children)
    assert all(child.embedding_token_count <= 180 for child in children)
    assert "".join(c.text for c in children) == source.text


def test_single_table_line_overflow_fails_instead_of_truncating() -> None:
    source = page("单位：元\n项目 2025年 2024年\n" + "内容" * 300 + " 123.45 678.90")
    parent, _ = split_page(source, ChunkConfig())
    with pytest.raises(ValueError, match="表格单行超限"):
        prepare_chunk(parent[0], {("doc", 1): source}, len, "test", "rev", 200)


def test_vector_validation_rejects_invalid_outputs() -> None:
    good = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    assert validate_vectors(good, 2, 2)["normalized"]
    with pytest.raises(ValueError, match="形状"):
        validate_vectors(good, 1, 2)
    with pytest.raises(ValueError, match="NaN"):
        validate_vectors(np.array([[np.nan, 0.0]]), 1, 2)
    with pytest.raises(ValueError, match="归一化"):
        validate_vectors(np.array([[2.0, 0.0]]), 1, 2)


def test_encode_refuses_overflow_before_calling_model() -> None:
    class FakeModel:
        def tokenizer(self, text, **kwargs):
            assert kwargs["truncation"] is False
            assert kwargs["add_special_tokens"] is True
            return {"input_ids": list(range(len(text) + 2))}

        def encode(self, *args, **kwargs):
            pytest.fail("超限输入不应进入模型编码")

    encoder = object.__new__(LocalEmbeddingModel)
    encoder.model = FakeModel()
    encoder.token_budget = 10
    with pytest.raises(ValueError, match="输入超限"):
        encoder.encode(["中文" * 10])
