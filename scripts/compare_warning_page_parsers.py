"""用独立pypdf读取警告页，对照已暂存的PyMuPDF文本，不把数字集合相等当作语义证明。"""

import json
import re
from collections import Counter
from pathlib import Path

import pypdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/table_audit"
NUMBER = re.compile(r"\(?[-+]?\d[\d,]*(?:\.\d+)?\)?%?")


def counts(text):
    # 不删列间空格：删掉空格会把相邻单元格的两个数字粘成一个假金额。
    return Counter(NUMBER.findall(text.replace("\u00a0", " ")))


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
    targets = [
        json.loads(line)
        for line in (OUT / "pages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["parser_warning"]
    ]
    texts = {
        (p["document_id"], p["page_number"]): p["text"]
        for p in (
            json.loads(line)
            for line in (OUT / "prepared/pages.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    reports = []
    for doc_id in dict.fromkeys(p["document_id"] for p in targets):
        reader = pypdf.PdfReader(ROOT / documents[doc_id]["local_path"])
        for page in [p for p in targets if p["document_id"] == doc_id]:
            number = page["page_number"]
            errors = []
            try:
                alternative = reader.pages[number - 1].extract_text(extraction_mode="layout")
                mode = "layout"
            except Exception as error:
                errors.append(f"layout: {type(error).__name__}: {error}")
                try:
                    alternative = reader.pages[number - 1].extract_text()
                    mode = "plain_fallback"
                except Exception as fallback_error:
                    errors.append(f"plain: {type(fallback_error).__name__}: {fallback_error}")
                    alternative, mode = "", "failed"
            primary = texts[(doc_id, number)]
            first, second = counts(primary), counts(alternative)
            reports.append(
                {
                    "document_id": doc_id,
                    "page_number": number,
                    "source_sha256": documents[doc_id]["sha256"],
                    "pypdf_version": pypdf.__version__,
                    "pymupdf_characters": len(primary),
                    "pypdf_mode": mode,
                    "pypdf_errors": errors,
                    "pypdf_characters": len(alternative),
                    "primary_only_numbers": dict(first - second),
                    "alternative_only_numbers": dict(second - first),
                    "pypdf_layout_text": alternative,
                    "status": "independent_parser_comparison_not_semantic_approval",
                }
            )
    (OUT / "warning_parser_comparison.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            [{k: v for k, v in r.items() if k != "pypdf_layout_text"} for r in reports],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
