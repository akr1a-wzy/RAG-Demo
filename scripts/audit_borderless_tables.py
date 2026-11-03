"""补查线框识别未覆盖的数字密集页；文本推断仅作为候选结构。"""

import json
from pathlib import Path

import pymupdf

from app.retrieval.table_structure import _merged_cells, _rounded

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/table_audit"


def main():
    documents = {
        d["document_id"]: d
        for d in (
            json.loads(line)
            for line in (ROOT / "data/manifests/documents.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    pages = [
        json.loads(line)
        for line in (OUT / "pages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    candidates = [p for p in pages if p["candidate_without_ruled_table"]]
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    records, failures = [], []
    for doc_id in dict.fromkeys(p["document_id"] for p in candidates):
        with pymupdf.open(ROOT / documents[doc_id]["local_path"]) as pdf:
            targets = [p for p in candidates if p["document_id"] == doc_id]
            for target in targets:
                page = pdf[target["page_number"] - 1]
                try:
                    found = page.find_tables(
                        strategy="text", min_words_vertical=3, min_words_horizontal=1
                    )
                    for number, table in enumerate(found.tables, 1):
                        if table.col_count < 2 or table.row_count < 3:
                            continue
                        records.append(
                            {
                                "document_id": doc_id,
                                "page_number": target["page_number"],
                                "table_number": number,
                                "bbox": _rounded(table.bbox),
                                "row_count": table.row_count,
                                "column_count": table.col_count,
                                "rows": table.extract(),
                                "merged_cells": _merged_cells(table),
                                "cell_bboxes": [
                                    [_rounded(c) for c in row.cells] for row in table.rows
                                ],
                                "detector_header_names": table.header.names,
                                "status": "borderless_text_geometry_candidate_unconfirmed",
                            }
                        )
                except Exception as error:
                    failures.append(
                        {
                            "document_id": doc_id,
                            "page_number": target["page_number"],
                            "error": str(error),
                        }
                    )
        print(f"DONE {doc_id} candidate_pages={len(targets)}", flush=True)
    (OUT / "borderless_candidates.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    report = {
        "pages_checked": len(candidates),
        "candidate_tables": len(records),
        "pages_with_candidates": len({(r["document_id"], r["page_number"]) for r in records}),
        "failures": failures,
        "scope": "无边框文本几何候选，未确认单元格语义或续表关系",
    }
    (OUT / "borderless_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
