"""在结构复核目录生成新切片，记录隔离原因，不发布活动检索索引。"""

import json
import os
from collections import Counter
from pathlib import Path

from app.retrieval.chunking import ChunkConfig, split_page, validate_chunks
from app.retrieval.embedding_prepare import (
    PREPARATION_VERSION,
    load_confirmed_headers,
    prepare_chunk,
)
from app.retrieval.pdf_extract import extract_document

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/table_audit/prepared"
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main():
    from transformers import AutoTokenizer

    manifest = json.loads((ROOT / "data/models/embedding_model.json").read_text(encoding="utf-8"))
    tokenizer = AutoTokenizer.from_pretrained(ROOT / manifest["local_path"], local_files_only=True)

    def count(text):
        return len(
            tokenizer(text, truncation=False, add_special_tokens=True, verbose=False)["input_ids"]
        )

    documents = [
        json.loads(line)
        for line in (ROOT / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    stats = Counter()
    quarantined, reviewed_examples = [], []
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    with (
        (OUT / "pages.jsonl").open("w", encoding="utf-8") as pf,
        (OUT / "chunks.jsonl").open("w", encoding="utf-8") as cf,
        (OUT / "embedding_chunks.jsonl").open("w", encoding="utf-8") as ef,
    ):
        for document in documents:
            pages = list(extract_document(ROOT, document))
            lookup = {(p.document_id, p.page_number): p for p in pages}
            confirmed = load_confirmed_headers(ROOT, document, lookup)
            section = ""
            for page in pages:
                pf.write(page.model_dump_json() + "\n")
                stats["pages"] += 1
                parents, section = split_page(page, ChunkConfig(), section)
                for parent in parents:
                    cf.write(parent.model_dump_json() + "\n")
                    stats["parents"] += 1
                    try:
                        children = prepare_chunk(
                            parent,
                            lookup,
                            count,
                            manifest["model_id"],
                            manifest["revision"],
                            480,
                            confirmed_headers=confirmed,
                        )
                        coverage = bytearray(len(parent.text))
                        for child in children:
                            offset = child.start_char - parent.start_char
                            if page.text[child.start_char : child.end_char] != child.text:
                                raise ValueError("子片段与来源偏移不一致")
                            coverage[offset : offset + len(child.text)] = b"\x01" * len(child.text)
                            if child.repeated_header:
                                header = child.repeated_header
                                if (
                                    lookup[(header.document_id, header.page_number)].text[
                                        header.start_char : header.end_char
                                    ]
                                    != header.text
                                ):
                                    raise ValueError("表头与来源偏移不一致")
                        if any(
                            not c.isspace() and not coverage[i] for i, c in enumerate(parent.text)
                        ):
                            raise ValueError("父片段非空白内容丢失")
                        if page.structure_review_required:
                            raise ValueError("PDF解析/文字定位警告页，需复核后解锁")
                        for child in children:
                            ef.write(child.model_dump_json() + "\n")
                            stats["children"] += 1
                            stats["max_tokens"] = max(
                                stats["max_tokens"], child.embedding_token_count
                            )
                            stats["local_header_children"] += child.repeated_header is not None
                            if page.document_id == "600519-2025-annual" and page.page_number == 12:
                                reviewed_examples.append(child.model_dump())
                    except ValueError as error:
                        quarantined.append(
                            {
                                "parent_chunk_id": parent.chunk_id,
                                "document_id": parent.document_id,
                                "page_number": parent.page_start,
                                "reason": str(error),
                                "start_char": parent.start_char,
                                "end_char": parent.end_char,
                                "text_preview": parent.text[:250],
                            }
                        )
            print(
                f"DONE {document['document_id']} total_children={stats['children']} "
                f"quarantined={len(quarantined)}",
                flush=True,
            )
    checks = validate_chunks(
        OUT / "pages.jsonl", OUT / "embedding_chunks.jsonl", ROOT / "eval/dataset.jsonl"
    )
    report = {
        "version": PREPARATION_VERSION,
        "documents": len(documents),
        **stats,
        "quarantined_parents": len(quarantined),
        "quarantined_pages": len({(q["document_id"], q["page_number"]) for q in quarantined}),
        "quarantine_reasons": dict(Counter(q["reason"].split(":")[0] for q in quarantined)),
        "source_evidence_validation": checks,
        "active_index_published": False,
        "scope": (
            "新切片暂存＋逐父片段覆盖/来源偏移检查＋真实模型token预算；语义未确认表仍须结构证据约束"
        ),
    }
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "quarantine.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in quarantined), encoding="utf-8"
    )
    (OUT / "known_header_fix.json").write_text(
        json.dumps(reviewed_examples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "source_evidence_validation"},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
