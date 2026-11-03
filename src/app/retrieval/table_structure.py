"""PDF表格版面审计：保存原始单元格、坐标、合并结构和待核查项。"""

from __future__ import annotations

import hashlib
import re

import pymupdf

AUDIT_VERSION = "pdf-table-geometry-v1"
UNIT = re.compile(r"单位\s*[:：]\s*[^\n]{1,60}")
NUMBER = re.compile(r"[-+]?\d[\d,]*\.\d+|\d+%")


def _rounded(rect):
    return [round(float(v), 2) for v in rect] if rect else None


def _merged_cells(table):
    """用实际矩形跨越的网格间隔识别合并；None保持为结构占位。"""
    cells = [cell for row in table.rows for cell in row.cells if cell]
    xs = sorted({round(c[i], 1) for c in cells for i in (0, 2)})
    ys = sorted({round(c[i], 1) for c in cells for i in (1, 3)})
    merged = []
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            if not cell:
                continue
            columns = sum(cell[0] - 0.2 <= x < cell[2] - 0.2 for x in xs)
            rows = sum(cell[1] - 0.2 <= y < cell[3] - 0.2 for y in ys)
            if columns > 1 or rows > 1:
                merged.append(
                    {
                        "row": r,
                        "column": c,
                        "row_span": rows,
                        "column_span": columns,
                        "bbox": _rounded(cell),
                    }
                )
    return merged


def audit_page(page, document_id, page_number):
    pymupdf.TOOLS.mupdf_warnings(reset=True)
    text = page.get_text("text", sort=True)
    words = page.get_text("words")
    found = page.find_tables(strategy="lines_strict")
    tables = sorted(
        [t for t in found.tables if t.row_count >= 2 and t.col_count >= 2],
        key=lambda t: (t.bbox[1], t.bbox[0]),
    )
    result = []
    previous_bottom = 0.0
    for position, table in enumerate(tables):
        bbox = table.bbox
        # 单位只从当前表上方、上一表底部之后的区域读取。
        # 同页上一张表的单位不能流入没有声明单位的下一张表。
        gap_top = max(previous_bottom, bbox[1] - 90)
        gap = page.get_textbox(pymupdf.Rect(0, gap_top, page.rect.width, bbox[1] + 2))
        matches = list(UNIT.finditer(gap))
        unit = matches[-1].group().strip() if matches else None
        rows = table.extract()
        merged = _merged_cells(table)
        title_lines = [
            line.strip()
            for line in gap.splitlines()
            if line.strip() and not UNIT.search(line) and len(line.strip()) <= 65
        ]
        title = title_lines[-1] if title_lines else None
        issues = []
        if merged:
            issues.append("merged_cells")
        if any(len(row) != table.col_count for row in rows):
            issues.append("ragged_grid")
        if table.header.external:
            issues.append("external_header")
        if not unit:
            issues.append("unit_not_explicit_in_local_gap")
        header_names = table.header.names
        if any(NUMBER.search(name or "") for name in header_names):
            issues.append("first_row_may_be_data_not_header")
        if bbox[1] < page.rect.height * 0.2:
            issues.append("page_top_table")
        if bbox[3] > page.rect.height * 0.85:
            issues.append("page_bottom_table")
        inside_words = [
            w
            for w in words
            if bbox[0] - 1 <= (w[0] + w[2]) / 2 <= bbox[2] + 1
            and bbox[1] - 1 <= (w[1] + w[3]) / 2 <= bbox[3] + 1
        ]
        table_text = "\n".join(" | ".join(cell or "" for cell in row) for row in rows)
        missing_numbers = sorted(
            {w[4] for w in inside_words if NUMBER.fullmatch(w[4]) and w[4] not in table_text}
        )
        if missing_numbers:
            issues.append("numeric_word_not_in_extracted_cells")
        identity = f"{document_id}:{page_number}:{position}:{_rounded(bbox)}:{AUDIT_VERSION}"
        result.append(
            {
                "table_id": hashlib.sha256(identity.encode()).hexdigest(),
                "document_id": document_id,
                "page_number": page_number,
                "table_number": position + 1,
                "bbox": _rounded(bbox),
                "page_width": page.rect.width,
                "page_height": page.rect.height,
                "row_count": table.row_count,
                "column_count": table.col_count,
                "rows": rows,
                "cell_bboxes": [[_rounded(cell) for cell in row.cells] for row in table.rows],
                "merged_cells": merged,
                "null_cells": sum(cell is None for row in rows for cell in row),
                "detector_header_names": header_names,
                "header_external": table.header.external,
                "header_bbox": _rounded(table.header.bbox),
                "title_candidate": title,
                "explicit_local_unit": unit,
                "unit_source_gap": gap,
                "missing_numeric_words": missing_numbers,
                "issues": issues,
                "status": "geometry_extracted_needs_semantic_review",
            }
        )
        previous_bottom = max(previous_bottom, bbox[3])
    warnings = pymupdf.TOOLS.mupdf_warnings(reset=True)
    dense = len(NUMBER.findall(text)) >= 8 or bool(UNIT.search(text))
    return result, {
        "document_id": document_id,
        "page_number": page_number,
        "table_count": len(result),
        "numeric_or_unit_candidate": dense,
        "candidate_without_ruled_table": dense and not result,
        "parser_warning": warnings or None,
        "text_characters": len(text),
    }


def continuation_candidates(previous, current):
    """仅提出候选；相同列数/坐标不构成已确认续表，不自动跨页复制单位。"""
    candidates = []
    for first in previous:
        if "page_bottom_table" not in first["issues"]:
            continue
        for second in current:
            if (
                "page_top_table" not in second["issues"]
                or first["column_count"] != second["column_count"]
            ):
                continue
            aligned = all(abs(first["bbox"][i] - second["bbox"][i]) < 8 for i in (0, 2))
            if aligned:
                candidates.append(
                    {
                        "from_table_id": first["table_id"],
                        "to_table_id": second["table_id"],
                        "document_id": first["document_id"],
                        "from_page": first["page_number"],
                        "to_page": second["page_number"],
                        "status": "candidate_requires_header_title_and_visual_check",
                    }
                )
    return candidates
