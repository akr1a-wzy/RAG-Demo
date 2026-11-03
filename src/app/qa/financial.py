"""已审结构化财务行的运行时适配；只开放选定年度、主体和原始单位。"""

import hashlib
import json
import re
from pathlib import Path

import pymupdf

from app.qa.facts import compact
from app.qa.requests import financial_request, requested_units

FINANCIAL_VERSION = "reviewed-financial-cells-v1"
QA_POLICY = "reviewed-revenue-v2-natural-employee-v3"


def supported_financial_request(resolved: dict) -> bool:
    """有限问题语法，未知附加条件不降格为总营收；不能忽略同比/季度/换算要求。"""
    return financial_request(resolved)["status"] == "supported"


class ReviewedFinancialFacts:
    def __init__(self, root: Path):
        self.root = root.resolve()
        path = self.root / "data/qa/reviewed_financial_cells.v1.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.reviews = payload.get("reviews", [])
        self.artifacts = payload.get("artifacts", {})
        manifest = self.root / "data/manifests/documents.jsonl"
        self.documents = {d["document_id"]: d for d in map(json.loads, manifest.read_text(
            encoding="utf-8").splitlines())} if self.reviews else {}

    def checked_path(self, item: dict, directory: str) -> Path:
        path = (self.root / item["path"]).resolve()
        if (not path.is_relative_to(self.root / directory)
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]):
            raise ValueError("财务审核产物路径或哈希不匹配")
        return path

    def facts_for(self, source: dict) -> list[dict]:
        matches = [r for r in self.reviews if (
            r["review_status"] == "selected_cell_visually_checked"
            and r["document_id"] == source.get("document_id")
            and r["stock_code"] == source.get("stock_code")
            and r["physical_page"] == source.get("physical_page")
            and r["period"] == source.get("report_period")
            and source.get("document_type") == "annual_report"
            and compact(r["row_quote"]) in compact(source["content"]))]
        if not matches:
            return []
        try:
            schemas = json.loads(self.checked_path(self.artifacts["schemas"], "data").read_text(
                encoding="utf-8"))
            row_text = self.checked_path(self.artifacts["rows"], "data").read_text(encoding="utf-8")
            rows = [json.loads(s) for s in row_text.splitlines() if s.strip()]
            return [fact for review in matches
                    if (fact := self.checked_fact(source, review, schemas, rows))]
        except (OSError, ValueError, KeyError, IndexError, TypeError, StopIteration):
            # 审核不完整则无可用事实；不能因读取失败退回猜列。
            return []

    def checked_fact(self, source, review, schemas, rows):
        if (review["metric"] != "revenue" or review["entity_scope"] != "consolidated_group"
                or review["accounting_basis"] != "as_reported" or review["currency"] != "CNY"
                or review["unit"] not in {"元", "千元"}):
            return None
        schema = next(s for s in schemas if s["table_id"] == review["table_id"])
        row = next(r for r in rows if r["table_id"] == review["table_id"]
                   and r["row_index"] == review["row_index"])
        for artifact in [schema, row]:
            if (artifact["document_id"] != source["document_id"]
                    or artifact["page_number"] != source["physical_page"]
                    or artifact["source_sha256"] != review["pdf_sha256"]):
                return None
        cell = row["cells"][review["column_index"]]
        if (cell["column_index"] != review["column_index"]
                or compact(cell["text"]) != compact(review["value"])
                or cell["value_unit"] != review["unit"]
                or cell["column_header_path"] != [review["period"] + "年"]
                or schema["column_paths"][review["column_index"]] != cell["column_header_path"]
                or compact(row["cells"][0]["text"]) != compact(review["row_label"])
                or compact(" ".join(c["text"] or "" for c in row["cells"])) !=
                compact(review["row_quote"])):
            return None
        doc = self.documents[review["document_id"]]
        if doc["sha256"] != review["pdf_sha256"]:
            return None
        path = self.checked_path({"path": doc["local_path"], "sha256": review["pdf_sha256"]},
                                 "data/raw")
        for image in review["images"]:
            self.checked_path(image, "docs/retrospective/evidence")
        proofs = {p["kind"]: p for p in review["proofs"]}
        if set(proofs) != {"row", "value", "year", "unit", "consolidated_corroboration"}:
            return None
        with pymupdf.open(path) as pdf:
            for proof in proofs.values():
                actual = pdf[proof["page"] - 1].get_textbox(pymupdf.Rect(proof["bbox"]))
                if compact(actual) != compact(proof["text"]):
                    return None
        value, year = proofs["value"], proofs["year"]
        if (any(proofs[k]["page"] != source["physical_page"]
                for k in ["row", "value", "year", "unit"])
                or value["bbox"] != cell["bbox"]
                or value["bbox"][::2] != year["bbox"][::2]
                or compact(year["text"]) != review["period"] + "年"
                or compact(value["text"]) != compact(review["value"])
                or compact(proofs["row"]["text"]) != compact(review["row_label"])):
            return None
        unit_text = compact(proofs["unit"]["text"])
        if not re.search(r"(?:单位:|营业收入\()" + review["unit"] + r"(?:\)|$|币种)", unit_text):
            return None
        corroboration = compact(proofs["consolidated_corroboration"]["text"])
        if not all(token in corroboration for token in [
            "合并利润表", "其中:营业收入", review["period"] + "年", "单位:" + review["unit"],
            compact(review["value"]),
        ]):
            return None
        return {
            "fact_id": review["review_id"] + ":" + source["source_id"],
            "review_id": review["review_id"], "version": FINANCIAL_VERSION,
            "metric": review["metric"], "row_label": review["row_label"],
            "period": review["period"], "value": review["value"], "unit": review["unit"],
            "entity_scope": review["entity_scope"], "accounting_basis": review["accounting_basis"],
            "currency": review["currency"], "unit_basis": "reviewed_pdf_row_or_table_unit",
            "unit_quote": proofs["unit"]["text"], "year_basis": "reviewed_pdf_year_column",
            "year_context_quote": year["text"], "year_context_origin": "reviewed_pdf_column",
            "header_quote": year["text"], "column_index": review["column_index"],
            "row_quote": review["row_quote"], "source_id": source["source_id"],
            "stock_code": source["stock_code"], "document_id": source["document_id"],
            "physical_page": source["physical_page"], "table_id": review["table_id"],
        }

    def allows_citation(self, source: dict, quote: str) -> bool:
        return any(compact(f["row_quote"]) in compact(quote) for f in self.facts_for(source))


def financial_fact_allowed(fact: dict, query: str) -> bool:
    """普通简单列解析不能替代财务审核，原文千元不能按问题要求悄悄换成元。"""
    units = requested_units(query)
    return (fact.get("version") == FINANCIAL_VERSION and bool(fact.get("review_id"))
            and fact.get("entity_scope") == "consolidated_group"
            and fact.get("accounting_basis") == "as_reported"
            and all(unit == fact["unit"] for unit in units))
