"""汇总已做的视觉判断及其可复核结构证据，不给未查看表自动盖章。"""

import json
from pathlib import Path

from app.retrieval.table_review import header_paths, structured_rows

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/table_audit"


def main():
    tables = [
        json.loads(line)
        for line in (OUT / "tables.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
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

    def table(doc, page, number=1):
        return next(
            t
            for t in tables
            if (t["document_id"], t["page_number"], t["table_number"]) == (doc, page, number)
        )

    root = table("600519-2025-annual", 56)
    configs = [
        ("600519-2025-annual", 12, 1, 1, "万元", None, None),
        ("600519-2025-annual", 12, 2, 0, "元", [["项目"], ["本期金额或比例"]], None),
        ("600519-2025-annual", 12, 3, 0, None, [["人员/结构项目"], ["数量或比例"]], None),
        ("600519-2025-annual", 56, 1, 1, "元", None, None),
        ("600519-2025-annual", 57, 1, 0, "元", header_paths(root, 1), root["table_id"]),
        ("600519-2025-annual", 58, 1, 0, "元", header_paths(root, 1), root["table_id"]),
        ("300750-2025-annual", 24, 1, 2, "千元", None, None),
        ("300750-2025-annual", 26, 1, 2, None, None, None),
    ]
    reviewed, rows = [], []
    for doc_id, page, number, headers, unit, columns, parent_id in configs:
        source = table(doc_id, page, number)
        output = structured_rows(source, headers, columns, unit)
        paths = columns or header_paths(source, headers)
        reviewed.append(
            {
                "table_id": source["table_id"],
                "document_id": doc_id,
                "page_number": page,
                "table_number": number,
                "source_sha256": documents[doc_id]["sha256"],
                "header_rows": headers,
                "column_paths": paths,
                "unit": unit,
                "header_source_table_id": parent_id,
                "grid_rows": source["row_count"],
                "grid_columns": source["column_count"],
                "merged_cells": source["merged_cells"],
                "status": "visual_structure_and_selected_cells_checked",
                "limits": "只展开矩形证明的合并引用；空白不补零；未逐格人工校对全表",
            }
        )
        for row in output:
            row.update(source_sha256=documents[doc_id]["sha256"], header_source_table_id=parent_id)
        rows.extend(output)
    # 这些是已完成的视觉判断；风险保留，不把无边框候选直接当标准金额表。
    decisions = [
        {
            "document_id": "000858-2025-annual",
            "pages": [35, 36],
            "decision": (
                "不关联为同一表；35页为内控定性/定量标准和审计表，"
                "36页为环境企业索引；无货币单位继承"
            ),
        },
        {
            "document_id": "600036-2025-annual",
            "pages": [14],
            "decision": (
                "无边框财务表确实存在，经营业绩、每股、百分比需分区域；"
                "整页文本表候选尚不批准作为统一金额表"
            ),
        },
        {
            "document_id": "601318-2025-annual",
            "pages": [15, 17],
            "decision": (
                "15页客户数量、百万金额、百分比、每股值混合；17页插图作为非表负例；均不自动补单位"
            ),
        },
        {
            "document_id": "600030-2025-annual",
            "pages": [17],
            "decision": (
                "业务许可时间轴，文本与数值页脚不能打包成一个超长表格行；"
                "保留文字，不宣称时间轴关系已恢复"
            ),
        },
        {
            "document_id": "000333-2025-annual",
            "pages": [10, 57],
            "decision": (
                "已查看原图、独立解析关键数字匹配；备用解析和锚点验收见"
                "pdf_page_reviews.json；百分比/金额不能全局同单位"
            ),
        },
        {
            "document_id": "600050-2025-annual",
            "pages": [77, 78, 79, 139, 141, 210],
            "decision": (
                "主解析79页实际遗漏首行金额；已备用解析并核对年份、合并/公司列、"
                "单位、关键金额；保留警告与锚点纠错证据，未证明全表语义一致"
            ),
        },
    ]
    (OUT / "reviewed_tables.json").write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "reviewed_rows.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    (OUT / "visual_decisions.json").write_text(
        json.dumps(decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"reviewed_table_schemas={len(reviewed)}, structured_rows={len(rows)}")
    print("CATL revenue column paths:", reviewed[6]["column_paths"])


if __name__ == "__main__":
    main()
