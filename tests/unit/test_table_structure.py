import hashlib
import json
from pathlib import Path

import pymupdf

from app.retrieval.chunking import ChunkConfig, split_page
from app.retrieval.embedding_prepare import prepare_chunk
from app.retrieval.pdf_extract import PageRecord
from app.retrieval.table_review import expand_merged_grid, header_paths
from app.retrieval.table_structure import audit_page, continuation_candidates


def make_page(text, number=1):
    return PageRecord(
        page_id=f"doc:p{number}",
        document_id="doc",
        stock_code="600519",
        stock_name="茅台",
        document_type="annual_report",
        title="测试年报",
        report_period="2025",
        publish_date="2026-04-17",
        source_url="https://example.org/",
        local_path="data/raw/test.pdf",
        page_number=number,
        text=text,
        character_count=len(text),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def children(source, lookup=None):
    parents, _ = split_page(source, ChunkConfig())
    return [
        child
        for parent in parents
        for child in prepare_chunk(
            parent, lookup or {("doc", source.page_number): source}, len, "test", "rev", 300
        )
    ]


def test_unit_does_not_cross_research_and_personnel_boundaries():
    source = make_page(
        "单位：万元\n项目 2025年 2024年\n贸易收入 551.68 7169.06\n\n"
        "(1).研发投入情况表\n单位：元\n研发投入合计 803132232.31\n\n"
        "(2).研发人员情况表\n公司研发人员的数量 806\n研发人员学历结构\n"
        "学历结构类别 学历结构人数\n博士研究生 88\n研发人员年龄结构\n"
        "年龄结构类别 年龄结构人数\n30岁以下 153"
    )
    result = children(source)
    trading = next(c for c in result if "贸易收入 551.68" in c.text)
    research = next(c for c in result if "研发投入合计 803132232.31" in c.text)
    personnel = next(c for c in result if "公司研发人员的数量 806" in c.text)
    education = next(c for c in result if "博士研究生 88" in c.text)
    assert "万元" in trading.repeated_header.text
    assert "单位：元" in research.repeated_header.text
    assert "万元" not in research.embedding_text
    assert "单位：" not in personnel.embedding_text
    assert "单位：" not in education.embedding_text
    assert "学历结构类别" in education.repeated_header.text
    assert all(source.text[c.start_char : c.end_char] == c.text for c in result)


def test_new_page_never_borrows_unconfirmed_currency_header():
    previous = make_page("单位：元\n项目 2025年 2024年\n营收 123.45 120.34")
    source = make_page("(2).研发人员情况表\n公司研发人员的数量 806", 2)
    result = children(source, {("doc", 1): previous, ("doc", 2): source})
    assert all(not c.repeated_header or c.repeated_header.page_number == 2 for c in result)
    assert all("单位：元" not in c.embedding_text for c in result)


def test_repeated_identical_unit_segments_have_distinct_ids_and_complete_coverage():
    source = make_page("单位：元\n营收 123.45\n\n单位：元\n营收 123.45")
    result = children(source)
    assert len({c.chunk_id for c in result}) == len(result)
    coverage = set(i for c in result for i in range(c.start_char, c.end_char))
    assert all(i in coverage for i, char in enumerate(source.text) if not char.isspace())


def test_real_known_wrong_header_page_regression():
    root = Path(__file__).resolve().parents[2]
    source = next(
        PageRecord.model_validate_json(line)
        for line in (root / "data/processed/pages.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["document_id"] == "600519-2025-annual"
        and json.loads(line)["page_number"] == 12
    )
    result = children(source, {(source.document_id, 12): source})
    research = [c for c in result if "803,132,232.31" in c.text]
    personnel = [c for c in result if "公司研发人员的数量" in c.text]
    assert research and personnel
    assert all(
        c.repeated_header and c.repeated_header.text.startswith("单位：元") for c in research
    )
    assert all("万元" not in c.embedding_text for c in research)
    assert all(
        "单位：元" not in c.embedding_text and "万元" not in c.embedding_text for c in personnel
    )


def test_merged_grid_preserves_null_and_bbox():
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=300)
        shape = page.new_shape()
        for y in (50, 80, 110):
            shape.draw_line((30, y), (270, y))
        for x in (30, 270):
            shape.draw_line((x, 50), (x, 110))
        shape.draw_line((150, 80), (150, 110))
        shape.finish(color=(0, 0, 0))
        shape.commit()
        page.insert_text((40, 70), "Merged title")
        page.insert_text((40, 100), "Revenue")
        page.insert_text((160, 100), "123.45")
        result, _ = audit_page(page, "doc", 1)
    assert result and result[0]["merged_cells"]
    assert result[0]["merged_cells"][0]["column_span"] == 2
    assert result[0]["rows"][0][1] is None
    assert "numeric_word_not_in_extracted_cells" not in result[0]["issues"]


def test_continuation_is_only_a_candidate_and_requires_edge_alignment():
    first = {
        "table_id": "a",
        "document_id": "doc",
        "page_number": 1,
        "column_count": 4,
        "bbox": [30, 50, 270, 290],
        "issues": ["page_bottom_table"],
    }
    second = {
        "table_id": "b",
        "page_number": 2,
        "column_count": 4,
        "bbox": [30, 10, 270, 200],
        "issues": ["page_top_table"],
    }
    result = continuation_candidates([first], [second])
    assert result[0]["status"] == "candidate_requires_header_title_and_visual_check"
    assert not continuation_candidates([first], [{**second, "column_count": 3}])


def test_multilevel_year_headers_use_only_declared_merge_references():
    table = {
        "column_count": 5,
        "rows": [
            ["项目", "2025年", None, "2024年", None],
            [None, "金额", "占比", "金额", "占比"],
            ["收入", "100", "50%", None, "40%"],
        ],
        "merged_cells": [
            {"row": 0, "column": 0, "row_span": 2, "column_span": 1, "bbox": [0, 0, 1, 2]},
            {"row": 0, "column": 1, "row_span": 1, "column_span": 2, "bbox": [1, 0, 3, 1]},
            {"row": 0, "column": 3, "row_span": 1, "column_span": 2, "bbox": [3, 0, 5, 1]},
        ],
    }
    grid, sources = expand_merged_grid(table)
    assert header_paths(table, 2) == [
        ["项目"],
        ["2025年", "金额"],
        ["2025年", "占比"],
        ["2024年", "金额"],
        ["2024年", "占比"],
    ]
    assert grid[2][3] is None
    assert len(sources) == 3


def test_confirmed_continuation_keeps_dates_not_asset_group_caption():
    from app.retrieval.embedding_prepare import load_confirmed_headers
    from app.retrieval.pdf_extract import extract_document

    root = Path(__file__).resolve().parents[2]
    document = json.loads(
        (root / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    pages = {(p.document_id, p.page_number): p for p in extract_document(root, document)}
    headers = load_confirmed_headers(root, document, pages)
    assert set(number for _, number in headers) == {57, 58}
    assert "2025 年12 月31 日" in headers[(document["document_id"], 58)].text
    assert "流动资产" not in headers[(document["document_id"], 58)].text


def test_timeline_chart_is_not_packed_into_numeric_table():
    from app.retrieval.pdf_extract import extract_document

    root = Path(__file__).resolve().parents[2]
    document = next(
        json.loads(line)
        for line in (root / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line)["document_id"] == "600030-2025-annual"
    )
    source = next(p for p in extract_document(root, document) if p.page_number == 17)
    result = children(source, {(source.document_id, 17): source})
    assert result
    coverage = set(i for c in result for i in range(c.start_char, c.end_char))
    assert all(i in coverage for i, char in enumerate(source.text) if not char.isspace())
