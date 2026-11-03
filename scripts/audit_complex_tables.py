"""审计全部公开PDF的复杂表格，保留结构JSONL和逐页解析警告。"""

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

import pymupdf

from app.retrieval.table_structure import AUDIT_VERSION, audit_page, continuation_candidates

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/table_audit"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", nargs="+")
    args = parser.parse_args()
    documents = [
        json.loads(line)
        for line in (ROOT / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    if args.codes:
        documents = [d for d in documents if d["stock_code"] in args.codes]
    OUT.mkdir(parents=True, exist_ok=True)
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    code_hash = hashlib.sha256(
        (ROOT / "src/app/retrieval/table_structure.py").read_bytes()
    ).hexdigest()
    reports, all_tables, all_pages, continuations = [], [], [], []
    started = time.monotonic()
    for document in documents:
        doc_id = document["document_id"]
        saved = OUT / "documents" / f"{doc_id}.json"
        profile = {
            "source_sha256": document["sha256"],
            "version": AUDIT_VERSION,
            "code_sha256": code_hash,
            "pymupdf": pymupdf.VersionBind,
        }
        data = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None
        if data and data["profile"] == profile:
            print(f"REUSE {doc_id}", flush=True)
        else:
            path = ROOT / document["local_path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"]:
                raise ValueError(f"源文件哈希不匹配: {doc_id}")
            tables, pages, links = [], [], []
            previous = []
            with pymupdf.open(path) as pdf:
                for number, page in enumerate(pdf, 1):
                    current, summary = audit_page(page, doc_id, number)
                    tables.extend(current)
                    pages.append(summary)
                    links.extend(continuation_candidates(previous, current))
                    previous = current
                    if number % 50 == 0:
                        print(f"SCAN {doc_id} {number}/{len(pdf)} tables={len(tables)}", flush=True)
            data = {
                "document": document,
                "profile": profile,
                "tables": tables,
                "pages": pages,
                "continuation_candidates": links,
            }
            saved.parent.mkdir(parents=True, exist_ok=True)
            temp = saved.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
            temp.replace(saved)
            print(f"DONE {doc_id} pages={len(pages)} tables={len(tables)}", flush=True)
        all_tables.extend(data["tables"])
        all_pages.extend(data["pages"])
        continuations.extend(data["continuation_candidates"])
        reports.append(
            {
                "document_id": doc_id,
                "stock_code": document["stock_code"],
                "stock_name": document["stock_name"],
                "pages": len(data["pages"]),
                "tables": len(data["tables"]),
                "source_sha256": document["sha256"],
            }
        )
    for name, records in (
        ("tables.jsonl", all_tables),
        ("pages.jsonl", all_pages),
        ("continuations.jsonl", continuations),
    ):
        temp = (OUT / name).with_suffix(".jsonl.tmp")
        temp.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8"
        )
        temp.replace(OUT / name)
    issues = Counter(issue for table in all_tables for issue in table["issues"])
    report = {
        "version": AUDIT_VERSION,
        "company_count": len({d["stock_code"] for d in documents}),
        "document_count": len(documents),
        "pages_scanned": len(all_pages),
        "tables_detected": len(all_tables),
        "merged_tables": sum(bool(t["merged_cells"]) for t in all_tables),
        "candidate_pages_without_ruled_table": sum(
            p["candidate_without_ruled_table"] for p in all_pages
        ),
        "parser_warning_pages": sum(bool(p["parser_warning"]) for p in all_pages),
        "continuation_candidates": len(continuations),
        "issue_counts": dict(issues),
        "documents": reports,
        "seconds": round(time.monotonic() - started, 2),
        "scope": "全页自动结构扫描；续表/无边框表及语义关联须进一步复核，不等于所有表格人工验收",
    }
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "documents"}, ensure_ascii=False, indent=2
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
