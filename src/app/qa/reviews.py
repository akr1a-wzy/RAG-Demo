"""仅对已审核的PDF单元格提供事实；不解除整页或其他表格的复核标记。"""

import hashlib
import json
from pathlib import Path

import pymupdf

from app.qa.facts import compact


class ReviewedEmployeeFacts:
    def __init__(self, root: Path):
        self.root = root.resolve()
        path = self.root / "data/qa/reviewed_employee_cells.v1.json"
        self.reviews = (json.loads(path.read_text(encoding="utf-8"))["reviews"]
                        if path.exists() else [])
        manifest = self.root / "data/manifests/documents.jsonl"
        self.documents = {d["document_id"]: d for d in map(
            json.loads, manifest.read_text(encoding="utf-8").splitlines())} if self.reviews else {}

    def facts_for(self, source: dict) -> list[dict]:
        result = []
        for review in self.reviews:
            if (review["review_status"] not in {
                "selected_cell_visually_checked", "selected_span_visually_checked",
            }
                    or review["document_id"] != source.get("document_id")
                    or review["stock_code"] != source.get("stock_code")
                    or review["period"] != source.get("report_period")
                    or review["physical_page"] != source.get("physical_page")
                    or compact(review["row_quote"]) not in compact(source["content"])):
                continue
            doc = self.documents.get(review["document_id"])
            if not doc or doc["sha256"] != review["pdf_sha256"]:
                continue
            path = (self.root / doc["local_path"]).resolve()
            image = (self.root / review["evidence_image"]).resolve()
            if (not path.is_relative_to(self.root / "data/raw")
                    or not image.is_relative_to(self.root)):
                continue
            if (hashlib.sha256(path.read_bytes()).hexdigest() != review["pdf_sha256"]
                    or hashlib.sha256(image.read_bytes()).hexdigest() !=
                    review["evidence_image_sha256"]):
                continue
            with pymupdf.open(path) as pdf:
                page = pdf[review["physical_page"] - 1]
                if any(compact(page.get_textbox(pymupdf.Rect(review[key]))) !=
                       compact(review["expected_" + text + "_text"])
                       for key, text in [("value_bbox", "value"), ("header_bbox", "header"),
                                         ("period_bbox", "period")]):
                    continue
            header_text = compact(review["expected_header_text"])
            period_text = compact(review["expected_period_text"])
            is_span = review.get("review_kind") == "employee_narrative_span"
            proof = (("本集团共有员工" + review["value"] + "人" in header_text
                      and "截至报告期末" in header_text and "报告期末" in period_text)
                     if is_span else ("员工人数(人)" in header_text
                                      and review["period"] + "年12月31日" in period_text))
            if compact(review["expected_value_text"]) != review["value"] or not proof:
                continue
            fact = {
                "fact_id": review["review_id"] + ":" + source["source_id"],
                "metric": "headcount_total", "row_label": review["row_label"],
                "period": review["period"], "value": review["value"], "unit": "人",
                "unit_basis": ("reviewed_narrative_unit" if is_span
                               else "reviewed_pdf_column_header"),
                "entity_scope": "group",
                "unit_quote": review["row_quote"] if is_span else review["expected_header_text"],
                "year_basis": "reviewed_report_end_span" if is_span else "reviewed_pdf_table_date",
                "row_quote": review["row_quote"],
                "year_context_quote": review["expected_period_text"],
                "year_context_origin": "reviewed_span" if is_span else "reviewed_pdf_table_date",
                "column_index": None if is_span else 1,
                "source_id": source["source_id"], "stock_code": source["stock_code"],
                "document_id": source["document_id"], "physical_page": source["physical_page"],
                "review_id": review["review_id"], "version": "reviewed-employee-cells-v1",
            }
            result.append(fact)
        return result

    def allows_citation(self, source: dict, quote: str) -> bool:
        return any(compact(f["row_quote"]) in compact(quote) for f in self.facts_for(source))


class ReviewedFacts:
    """组合不同业务的审核事实源，保留每个单元格的独立放行范围。"""

    def __init__(self, root: Path):
        from app.qa.financial import ReviewedFinancialFacts

        self.providers = [ReviewedEmployeeFacts(root), ReviewedFinancialFacts(root)]

    def facts_for(self, source: dict) -> list[dict]:
        return [fact for provider in self.providers for fact in provider.facts_for(source)]

    def allows_citation(self, source: dict, quote: str) -> bool:
        return any(provider.allows_citation(source, quote) for provider in self.providers)
