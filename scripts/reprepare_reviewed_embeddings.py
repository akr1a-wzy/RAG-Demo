"""用实际SentenceTransformer分词器复核并重建审计子片段。"""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from app.retrieval.chunking import ChunkRecord, validate_chunks
from app.retrieval.embedding_prepare import EmbeddingChunk, load_confirmed_headers, prepare_chunk
from app.retrieval.pdf_extract import PageRecord

ROOT = Path(__file__).resolve().parents[1]
PREPARED = ROOT / "data/table_audit/prepared"
EVIDENCE = (
    ROOT / "docs/retrospective/evidence/reviewed-token-count-drift-2026-10-14.json"
)
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def records(path: Path, model):
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    from app.retrieval.embeddings import LocalEmbeddingModel

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    model_manifest = json.loads(
        (ROOT / "data/models/embedding_model.json").read_text(encoding="utf-8")
    )
    model = LocalEmbeddingModel(ROOT / model_manifest["local_path"])
    old_children = records(PREPARED / "embedding_chunks.jsonl", EmbeddingChunk)
    actual_counts = [model.count_tokens(item.embedding_text) for item in old_children]
    mismatches = [
        {
            "chunk_id": item.chunk_id,
            "parent_chunk_id": item.parent_chunk_id,
            "document_id": item.document_id,
            "page": item.page_start,
            "stored_tokens": item.embedding_token_count,
            "actual_tokens": actual,
        }
        for item, actual in zip(old_children, actual_counts, strict=True)
        if actual != item.embedding_token_count
    ]
    evidence = {
        "status": "confirmed_preparation_tokenizer_drift",
        "old_children": len(old_children),
        "stored_max_tokens": max(item.embedding_token_count for item in old_children),
        "actual_max_tokens": max(actual_counts),
        "mismatch_count": len(mismatches),
        "overflow_count": sum(count > model.token_budget for count in actual_counts),
        "token_budget": model.token_budget,
        "examples": sorted(mismatches, key=lambda item: item["actual_tokens"], reverse=True)[:20],
        "failure": "candidate build stopped before ingestion: Embedding输入超限: 497 > 480",
    }
    EVIDENCE.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not args.apply:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return

    pages = records(PREPARED / "pages.jsonl", PageRecord)
    parents = records(PREPARED / "chunks.jsonl", ChunkRecord)
    documents = {
        item["document_id"]: item
        for item in (
            json.loads(line)
            for line in (ROOT / "data/manifests/documents.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    page_lookup = {(item.document_id, item.page_number): item for item in pages}
    by_document: dict[str, list[ChunkRecord]] = {}
    for parent in parents:
        by_document.setdefault(parent.document_id, []).append(parent)
    children = []
    known_header_fix = []
    stats = Counter()
    for document_id, document_parents in by_document.items():
        confirmed = load_confirmed_headers(
            ROOT,
            documents[document_id],
            {
                key: page
                for key, page in page_lookup.items()
                if key[0] == document_id
            },
        )
        for parent in document_parents:
            result = prepare_chunk(
                parent,
                page_lookup,
                model.count_tokens,
                model_manifest["model_id"],
                model_manifest["revision"],
                model.token_budget,
                confirmed_headers=confirmed,
            )
            children.extend(result)
            stats["split_parents"] += len(result) > 1
            stats["local_header_children"] += sum(
                item.repeated_header is not None for item in result
            )
            if document_id == "600519-2025-annual" and parent.page_start == 12:
                known_header_fix.extend(item.model_dump() for item in result)
    temporary = PREPARED / "embedding_chunks.jsonl.tmp"
    temporary.write_text(
        "\n".join(item.model_dump_json() for item in children) + "\n", encoding="utf-8"
    )
    old_validation = validate_chunks(
        PREPARED / "pages.jsonl", temporary, ROOT / "eval/dataset.jsonl"
    )
    new_validation = validate_chunks(
        PREPARED / "pages.jsonl", temporary, ROOT / "eval/dataset_v2.jsonl"
    )
    if (
        old_validation["integrity_errors"]
        or old_validation["answerable_fail"]
        or new_validation["integrity_errors"]
        or new_validation["answerable_fail"]
    ):
        temporary.unlink(missing_ok=True)
        raise ValueError("重切后完整性或来源证据校验失败")
    temporary.replace(PREPARED / "embedding_chunks.jsonl")
    (PREPARED / "known_header_fix.json").write_text(
        json.dumps(known_header_fix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path = PREPARED / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "children": len(children),
            "max_tokens": max(item.embedding_token_count for item in children),
            "local_header_children": stats["local_header_children"],
            "source_evidence_validation": old_validation,
            "dataset_v2_evidence_validation": new_validation,
            "tokenizer_repreparation": {
                "status": "reprepared_with_runtime_sentence_transformer_tokenizer",
                "previous_children": len(old_children),
                "current_children": len(children),
                "previous_actual_overflow_count": evidence["overflow_count"],
                "failure_evidence": EVIDENCE.relative_to(ROOT).as_posix(),
            },
        }
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "children": len(children),
                "max_tokens": report["max_tokens"],
                "old_answerable_pass": old_validation["answerable_pass"],
                "new_answerable_pass": new_validation["answerable_pass"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
