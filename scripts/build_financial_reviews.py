"""重建已人工核对的4个营业收入单元格；复用旧结构化行格式，不导入评测金标准。"""

import hashlib
import json
from pathlib import Path

import pymupdf
from pypdf import PdfReader

from app.qa.facts import compact
from app.retrieval.table_review import structured_rows

ROOT = Path(__file__).resolve().parents[1]
# 主表物理页、人工核对列边界、表头/收入行边界、合并利润表物理页。
SPECS = [
    ("000858", 7, [62.5, 168.6, 260.8, 353, 447.9, 544.3], [188.6, 205.8, 222.5], 56),
    ("300750", 11, [56.8, 152.1, 252.9, 348.1, 443.3, 538.4], [330.5, 347.45, 364.73], 116),
    ("600276", 6, [84.42, 203.27, 295.42, 387.57, 444.27, 536.84], [177.84, 219.02, 233.89], 135),
    ("600887", 7, [48.03, 176.75, 273.83, 372.05, 449.5, 547.22], [300.02, 323.99, 342.94], 86),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    documents = {d["document_id"]: d for d in map(json.loads, (
        ROOT / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines())}
    schemas, rows, reviews = [], [], []
    evidence = ROOT / "docs/retrospective/evidence/phase14-financial"
    evidence.mkdir(parents=True, exist_ok=True)
    for code, page_number, xs, ys, scope_page in SPECS:
        document = documents[code + "-2025-annual"]
        pdf_path = ROOT / document["local_path"]
        assert sha(pdf_path) == document["sha256"]
        with pymupdf.open(pdf_path) as pdf:
            page = pdf[page_number - 1]
            boxes = [[[xs[c], ys[r], xs[c + 1], ys[r + 1]] for c in range(5)]
                     for r in range(2)]
            grid = [[page.get_textbox(pymupdf.Rect(b)).strip() for b in row] for row in boxes]
            assert compact(grid[0][1]) == "2025年" and compact(grid[0][2]) == "2024年"
            assert compact(grid[1][0]) in {"营业收入", "营业收入(元)"}
            unit = "千元" if code == "300750" else "元"
            unit_bbox = (boxes[1][0] if code == "000858" else
                         list(page.search_for("单位")[0] + (-1, -2, 70, 2)))
            unit_text = page.get_textbox(pymupdf.Rect(unit_bbox)).strip()
            assert unit in compact(unit_text)
            table_id = f"qa-financial-{code}-p{page_number}-revenue-v1"
            table = {"document_id": document["document_id"], "page_number": page_number,
                     "table_id": table_id, "rows": grid, "cell_bboxes": boxes,
                     "column_count": 5, "merged_cells": []}
            paths = [[compact(cell)] for cell in grid[0]]
            # 增减列没有百分号的样式也不继承金额单位。
            paths[3] = [compact(grid[0][3]) + ("" if "%" in compact(grid[0][3]) else "(%)")]
            extracted = structured_rows(table, 1, paths, unit)
            for row in extracted:
                row["source_sha256"] = document["sha256"]
            rows.extend(extracted)
            schemas.append({"table_id": table_id, "document_id": document["document_id"],
                            "page_number": page_number, "source_sha256": document["sha256"],
                            "column_paths": paths, "unit": unit, "header_rows": 1,
                            "status": "selected_revenue_cell_only", "merged_cells": []})
            # 独立解析器只校验选定行，不把同解析器的两个输出当独立证明。
            independent = PdfReader(pdf_path).pages[page_number - 1].extract_text()
            assert compact(grid[1][1]) in compact(independent) and "营业收入" in independent
            scope = pdf[scope_page - 1]
            label_box = scope.search_for("合并利润表")[0]
            value_boxes = scope.search_for(grid[1][1])
            assert value_boxes
            scope_row_y = value_boxes[-1].y0
            scope_row_box = [20, scope_row_y - 1, scope.rect.width - 20, value_boxes[-1].y1 + 1]
            scope_row = scope.get_textbox(pymupdf.Rect(scope_row_box)).strip()
            assert "营业收入" in compact(scope_row) and grid[1][1] in scope_row
            # 日期、单位、收入行和合并标题一并保留坐标证据。
            scope_context = [20, label_box.y0 - 1, scope.rect.width - 20, value_boxes[-1].y1 + 1]
            context_text = scope.get_textbox(pymupdf.Rect(scope_context)).strip()
            assert "2025" in context_text and unit in context_text
            main_image = evidence / f"{code}-main-p{page_number}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(
                xs[0] - 2, min(ys[0] - 70, unit_bbox[1] - 2), xs[-1] + 2, ys[-1] + 4
            )).save(main_image)
            scope_image = evidence / f"{code}-consolidated-p{scope_page}.png"
            scope.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(scope_context)).save(
                scope_image)
            reviews.append({
                "review_id": table_id + "-r1-c1", "review_status": "selected_cell_visually_checked",
                "reviewer": "same_assistant_pdf_visual_and_independent_text_review",
                "document_id": document["document_id"], "stock_code": code, "period": "2025",
                "physical_page": page_number, "pdf_sha256": document["sha256"],
                "table_id": table_id, "row_index": 1, "column_index": 1,
                "metric": "revenue", "entity_scope": "consolidated_group",
                "accounting_basis": "as_reported", "currency": "CNY", "unit": unit,
                "value": grid[1][1], "row_label": grid[1][0],
                "row_quote": " ".join(grid[1]),
                "proofs": [
                    {"page": page_number, "bbox": boxes[1][0], "text": grid[1][0], "kind": "row"},
                    {"page": page_number, "bbox": boxes[1][1], "text": grid[1][1], "kind": "value"},
                    {"page": page_number, "bbox": boxes[0][1], "text": grid[0][1], "kind": "year"},
                    {"page": page_number, "bbox": unit_bbox, "text": unit_text, "kind": "unit"},
                    {"page": scope_page, "bbox": scope_context, "text": context_text,
                     "kind": "consolidated_corroboration"},
                ],
                "images": [{"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p)}
                           for p in [main_image, scope_image]],
                "independent_parser": "pypdf-6.12.1", "independent_value_verified": True,
                "limits": "Only selected 2025 consolidated revenue; other cells not accepted",
            })
    paths = {"schemas": ROOT / "data/qa/financial_tables.v1.json",
             "rows": ROOT / "data/qa/financial_rows.v1.jsonl"}
    paths["schemas"].write_text(json.dumps(schemas, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    paths["rows"].write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                            encoding="utf-8")
    payload = {"version": "reviewed-financial-cells-v1", "artifacts": {
        key: {"path": path.relative_to(ROOT).as_posix(), "sha256": sha(path)}
        for key, path in paths.items()}, "reviews": reviews}
    (ROOT / "data/qa/reviewed_financial_cells.v1.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"schemas": len(schemas), "rows": len(rows), "selected_cells": len(reviews)}))


if __name__ == "__main__":
    build()
