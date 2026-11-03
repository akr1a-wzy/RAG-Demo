"""从原PDF建立答案金标准；与运行时事实解析器、检索输出和生成模型无关。"""

import hashlib
import json
import logging
import re
from pathlib import Path

import pymupdf
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/retrospective/evidence/phase9-gold"
# 人工选定PDF物理页和原始行；不得按待测系统的输出选择或剔除。
STAFF = {
    "600519": (31, "34,992", "在职员工的数量合计", "人"),
    "000858": (31, "25,216", "报告期末在职员工的数量合计", "人"),
    "000568": (41, "3,912", "报告期末在职员工的数量合计", "人"),
    "600036": (103, "121,585", "本集团共有员工", "人"),
    "600028": (47, "351,104", "本公司拥有员工", "名"),
    "601088": (84, "91,392", "本集团在职员工的数量合计", "人"),
    "600030": (83, "26,823", "本集团共有员工", "人"),
    "000333": (77, "198,570", "报告期末在职员工的数量合计", "人"),
    "002594": (69, "869,622", "报告期末在职员工的数量合计", "人"),
    "300750": (56, "185,839", "报告期末在职员工的数量合计", "人"),
    "601318": (157, "258,806", "平安体系内共有在职员工", "人"),
    "600900": (38, "8,482", "在职员工的数量合计", "人"),
    "601857": (90, "367,173", "合计", "人"),
    "000651": (50, "71,966", "报告期末在职员工的数量合计", "人"),
    "600276": (105, "20,602", "在职员工的数量合计", "人"),
    "600887": (46, "62,666", "在职员工的数量合计", "人"),
    "601012": (58, "32,775", "在职员工的数量合计", "人"),
    "002415": (113, "57,202", "报告期末在职员工的数量合计", "人"),
    "600050": (40, "237,251", "在职员工的数量合计", "人"),
    "600019": (50, "46,135", "在职员工的数量合计", "人"),
}
FINANCIAL = {
    "000858": (7, "40,528,509,770.23", "元"),
    "300750": (11, "423,701,834", "千元"),
    "600276": (6, "31,629,416,193.83", "元"),
    "600887": (7, "115,636,231,250.05", "元"),
}


def clean(text):
    return re.sub(r"\s+", "", text)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    target = ROOT / "eval/answer_acceptance_v1_gold.json"
    if target.exists():
        raise RuntimeError("金标准已存在，拒绝覆盖")
    OUT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=OUT / "parser-warnings.log", level=logging.WARNING)
    docs = {d["stock_code"]: d for d in map(json.loads, (
        ROOT / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines())
        if d["document_type"] == "annual_report"}
    facts = []
    for metric, spec in [("headcount_total", STAFF), ("revenue", FINANCIAL)]:
        for code, fields in spec.items():
            page, value, label, raw_unit = (fields if metric == "headcount_total"
                                             else (*fields[:2], "营业收入", fields[2]))
            doc = docs[code]
            pdf_path = ROOT / doc["local_path"]
            assert sha(pdf_path) == doc["sha256"], code
            reader = PdfReader(pdf_path)
            independent = reader.pages[page - 1].extract_text() or ""
            with pymupdf.open(pdf_path) as pdf:
                p = pdf[page - 1]
                primary = p.get_text(sort=True)
                assert clean(label) in clean(primary) and value in clean(primary), code
                matches = p.search_for(value)
                assert matches, code
                rect = matches[0]
                crop = pymupdf.Rect(0, max(0, rect.y0 - 90), p.rect.width,
                                     min(p.rect.height, rect.y1 + 110))
                image_path = OUT / f"{code}-{metric}-p{page}.png"
                p.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=crop).save(image_path)
            fact_id = f"{code}:2025:{metric}"
            text_path = OUT / f"{code}-{metric}-p{page}-pypdf.txt"
            text_path.write_text(independent, encoding="utf-8")
            facts.append({
                "fact_id": fact_id, "stock_code": code, "stock_name": doc["stock_name"],
                "period": "2025", "metric": metric, "value": value,
                "unit": "人" if metric == "headcount_total" else raw_unit,
                "raw_unit": raw_unit, "row_label": label,
                "document_id": doc["document_id"], "physical_page": page,
                "pdf_path": doc["local_path"], "pdf_sha256": doc["sha256"],
                "source_url": doc["source_url"], "independent_page_text": independent,
                "independent_label_verified": clean(label) in clean(independent),
                "independent_value_verified": value in clean(independent),
                "primary_page_text_sha256": hashlib.sha256(primary.encode()).hexdigest(),
                "screenshot": image_path.relative_to(ROOT).as_posix(),
                "unit_basis": "employee_count_semantics" if metric == "headcount_total"
                              else "explicit_pdf_header_or_row",
                "required_context": ["派遣"] if code == "600036" and metric == "headcount_total"
                                    else ["体系"] if code == "601318"
                                    and metric == "headcount_total"
                                    else [],
            })
    target.write_text(json.dumps({"version": "pdf-gold-v1", "visual_review": "pending",
                                 "independent_of_runtime_parser": True, "facts": facts},
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"facts": len(facts), "source_crops": len(facts), "target": str(target)}))


if __name__ == "__main__":
    main()
