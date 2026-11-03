import json
from pathlib import Path

from app.retrieval.pdf_extract import extract_document


def test_warning_page_recovers_missing_financial_rows_and_year_columns():
    root = Path(__file__).resolve().parents[2]
    document = next(
        json.loads(line)
        for line in (root / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line)["document_id"] == "600050-2025-annual"
    )
    page = next(p for p in extract_document(root, document) if p.page_number == 79)
    assert page.extraction_method == "pypdf_layout_fallback"
    assert not page.structure_review_required
    assert page.extraction_warnings
    for value in ("392,222,880,560", "389,589,219,642", "299,905,867,750", "2025年度", "2024年度"):
        assert value in page.text.replace(" ", "")


def test_unreviewed_warning_page_stays_blocked(tmp_path):
    root = Path(__file__).resolve().parents[2]
    document = next(
        json.loads(line)
        for line in (root / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line)["document_id"] == "600050-2025-annual"
    )
    # 同一真实PDF，空复核目录；绝对路径只用于只读回归样例。
    document["local_path"] = str(root / document["local_path"])
    page = next(p for p in extract_document(tmp_path, document) if p.page_number == 79)
    assert page.structure_review_required
    assert "392,222,880,560" in page.text
