import hashlib
import json

import pytest

from app.retrieval.chunking import ChunkConfig, chunk_pages, split_page, validate_chunks
from app.retrieval.pdf_extract import PageRecord


def make_page(text: str, document_id: str = "test-annual", page_number: int = 1) -> PageRecord:
    return PageRecord(
        page_id=f"{document_id}:p{page_number}",
        document_id=document_id,
        stock_code="600519",
        stock_name="贵州茅台",
        document_type="annual_report",
        title="测试年报",
        report_period="2025",
        publish_date="2026-04-17",
        source_url="https://example.org/annual.pdf",
        local_path="data/raw/test.pdf",
        page_number=page_number,
        text=text,
        character_count=len(text),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def test_prose_is_bounded_overlaps_and_preserves_source_positions() -> None:
    page = make_page("持续改进渠道服务能力" * 150)
    chunks, _ = split_page(page, ChunkConfig(200, 30))
    assert len(chunks) > 1
    assert all(c.character_count <= 200 for c in chunks)
    assert all(page.text[c.start_char : c.end_char] == c.text for c in chunks)
    assert all(a.end_char > b.start_char for a, b in zip(chunks, chunks[1:], strict=False))
    assert chunks[-1].end_char == len(page.text)


def test_table_is_preserved_and_oversized_is_explicit() -> None:
    table = "单位：元\n项目 2025年 2024年\n" + "营业收入 123,456.78 112,345.67\n" * 30
    page = make_page("一、财务情况\n\n" + table + "\n\n公司持续提升经营管理水平。")
    chunks, _ = split_page(page, ChunkConfig(200, 30))
    tables = [c for c in chunks if c.chunk_type == "table_candidate"]
    assert len(tables) == 1
    assert tables[0].text == table.strip()
    assert tables[0].oversized
    assert "单位：元" in tables[0].text


def test_heading_inheritance_and_document_boundary() -> None:
    first, section = split_page(make_page("一、业务情况\n\n公司经营酒类业务。"), ChunkConfig())
    next_page, _ = split_page(
        make_page("公司开展渠道建设。", page_number=2), ChunkConfig(), section
    )
    other, _ = split_page(make_page("另一份文档。", "other"), ChunkConfig())
    assert first[0].section == next_page[0].section == "一、业务情况"
    assert other[0].section == ""
    assert next_page[0].page_start == next_page[0].page_end == 2


def test_ids_are_stable_and_change_with_configuration() -> None:
    page = make_page("公司经营酒类业务。")
    first, _ = split_page(page, ChunkConfig())
    repeated, _ = split_page(page, ChunkConfig())
    changed, _ = split_page(page, ChunkConfig(600, 80))
    assert first[0].chunk_id == repeated[0].chunk_id
    assert first[0].chunk_id != changed[0].chunk_id


def test_small_sections_are_packed_with_their_headings() -> None:
    page = make_page("一、业务情况\n\n公司经营酒类业务。\n\n二、销售渠道\n\n采用经销模式。")
    chunks, _ = split_page(page, ChunkConfig())
    assert len(chunks) == 1
    assert chunks[0].section_paths == ["一、业务情况", "二、销售渠道"]
    assert chunks[0].text == page.text


def test_major_chapter_is_a_boundary() -> None:
    page = make_page("第一节 业务情况\n\n公司经营酒类业务。\n\n第二节 财务情况\n\n营业收入稳定。")
    chunks, _ = split_page(page, ChunkConfig())
    assert len(chunks) == 2
    assert chunks[0].section == "第一节 业务情况"
    assert chunks[1].section == "第二节 财务情况"


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, -1), (100, 100), (100, 101)])
def test_invalid_config(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        ChunkConfig(size, overlap)


def test_validation_detects_missing_evidence_and_excludes_refusal(tmp_path) -> None:
    page = make_page("营业收入 123,456.78元，保持稳定。")
    pages = tmp_path / "pages.jsonl"
    output = tmp_path / "chunks.jsonl"
    dataset = tmp_path / "dataset.jsonl"
    pages.write_text(page.model_dump_json() + "\n", encoding="utf-8")
    cases = [
        {
            "id": "1",
            "question": "营收？",
            "answerable": True,
            "expected_sources": [{"document_id": page.document_id, "page": 1}],
            "expected_keywords": ["123,456.78", "增长最高"],
        },
        {"id": "2", "answerable": False},
    ]
    dataset.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
    chunk_pages(pages, output, ChunkConfig())
    checks = validate_chunks(pages, output, dataset)
    assert checks["integrity_errors"] == []
    assert checks["answerable_pass"] == 1
    assert checks["refusal_not_evaluated"] == 1
    assert checks["cases"][0]["keywords_not_literal_in_source"] == ["增长最高"]
    output.write_text("", encoding="utf-8")
    checks = validate_chunks(pages, output, dataset)
    assert checks["integrity_errors"]
    assert checks["answerable_fail"] == 1


def test_duplicate_input_does_not_replace_existing_output(tmp_path) -> None:
    page = make_page("公司资料。")
    pages, output = tmp_path / "pages.jsonl", tmp_path / "chunks.jsonl"
    pages.write_text((page.model_dump_json() + "\n") * 2, encoding="utf-8")
    output.write_text("原有结果", encoding="utf-8")
    with pytest.raises(ValueError, match="重复页面ID"):
        chunk_pages(pages, output, ChunkConfig())
    assert output.read_text(encoding="utf-8") == "原有结果"


def test_repeated_footer_is_located_at_page_end_without_truncation() -> None:
    source = make_page("业务发展1000万目标。" * 180 + "\n\n10")
    chunks, _ = split_page(source, ChunkConfig())
    covered = set(i for c in chunks for i in range(c.start_char, c.end_char))
    assert all(i in covered for i, char in enumerate(source.text) if not char.isspace())
    assert max(c.end_char for c in chunks) == len(source.text)


def test_expanded_pdf_footer_regressions_preserve_all_nonblank_characters() -> None:
    from pathlib import Path

    from app.retrieval.pdf_extract import extract_document

    root = Path(__file__).resolve().parents[2]
    documents = [
        json.loads(line)
        for line in (root / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    for doc_id, number in (("601088-2025-annual", 3), ("600050-2025-annual", 10)):
        document = next(d for d in documents if d["document_id"] == doc_id)
        source = next(p for p in extract_document(root, document) if p.page_number == number)
        chunks, _ = split_page(source, ChunkConfig())
        covered = set(i for c in chunks for i in range(c.start_char, c.end_char))
        assert all(i in covered for i, char in enumerate(source.text) if not char.isspace())
