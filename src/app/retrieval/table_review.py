"""按已保存的合并矩形展开引用；空白格不以邻格值补齐。"""

import re
from copy import deepcopy


def expand_merged_grid(table):
    grid = deepcopy(table["rows"])
    inherited = []
    for merge in table["merged_cells"]:
        r, c = merge["row"], merge["column"]
        value = table["rows"][r][c]
        for row in range(r, min(r + merge["row_span"], len(grid))):
            for column in range(c, min(c + merge["column_span"], len(grid[row]))):
                if (row, column) != (r, c) and grid[row][column] is None:
                    grid[row][column] = value
                    inherited.append(
                        {
                            "row": row,
                            "column": column,
                            "source_row": r,
                            "source_column": c,
                            "source_bbox": merge["bbox"],
                        }
                    )
    return grid, inherited


def header_paths(table, rows):
    expanded, _ = expand_merged_grid(table)
    return [
        [value for index, value in enumerate(values) if value and value not in values[:index]]
        for values in (
            [expanded[row][column] for row in range(rows)]
            for column in range(table["column_count"])
        )
    ]


def structured_rows(table, header_rows, column_names=None, unit=None):
    expanded, inherited = expand_merged_grid(table)
    paths = column_names or header_paths(table, header_rows)
    result = []
    for number in range(header_rows, len(expanded)):
        row = expanded[number]
        cells = []
        for column, value in enumerate(row):
            original = table["rows"][number][column]
            ancestor = next(
                (item for item in inherited if item["row"] == number and item["column"] == column),
                None,
            )
            numeric = bool(value and re.fullmatch(r"\(?[-+]?\d[\d,]*(?:\.\d+)?\)?%?", value))
            percent = (
                bool(value and "%" in value)
                or "%" in (row[0] or "")
                or any("%" in p or "占比" in p or "比重" in p for p in paths[column])
            )
            value_unit = (
                None
                if not numeric or column == 0 or "附注" in paths[column]
                else ("%" if percent else unit)
            )
            cells.append(
                {
                    "column_index": column,
                    "column_header_path": paths[column],
                    "text": value,
                    "original_text": original,
                    "merged_reference": ancestor,
                    "bbox": table["cell_bboxes"][number][column],
                    "value_unit": value_unit,
                }
            )
        result.append(
            {
                "document_id": table["document_id"],
                "page_number": table["page_number"],
                "table_id": table["table_id"],
                "row_index": number,
                "cells": cells,
                "status": "reviewed_structure_selected_cells_only",
            }
        )
    return result
