"""将结构审计后的切片构建为候选索引，评测过线后才激活。"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from uuid import uuid4

import numpy as np

from app.retrieval.chroma_store import ingest_vectors
from app.retrieval.chunking import ChunkRecord, validate_chunks
from app.retrieval.embedding_prepare import EmbeddingChunk
from app.retrieval.embeddings import validate_vectors
from app.retrieval.incremental import (
    DOCUMENT_FIELDS,
    EXPORTS,
    IncrementalUpdater,
    _fingerprint,
    _records,
    _write_json,
    _write_records,
)
from app.retrieval.pdf_extract import PageRecord, extract_document, file_sha256

OLD_RECALL_GATE = 0.7037037037037037
OLD_ALL_SOURCES_GATE = 12
NEW_RECALL_GATE = 0.85
NEW_ALL_SOURCES_GATE = 15


class ReviewedIndexUpdater(IncrementalUpdater):
    """使用data/table_audit/prepared，不重新生成切片。"""

    def __init__(self, root: Path, model_factory=None) -> None:
        self.reviewed_root = root.resolve() / "data/table_audit/prepared"
        super().__init__(root, model_factory=model_factory)
        required = (
            "pages.jsonl",
            "chunks.jsonl",
            "embedding_chunks.jsonl",
            "quarantine.jsonl",
            "report.json",
        )
        missing = [name for name in required if not (self.reviewed_root / name).is_file()]
        if missing:
            raise ValueError(f"审计产物不完整: {missing}")
        self.reviewed_hashes = {
            name: file_sha256(self.reviewed_root / name) for name in required
        }
        self.profile.update(
            {
                "pipeline_version": "reviewed-staging-index-v1",
                "reviewed_bundle_hashes": self.reviewed_hashes,
            }
        )
        self.profile["code_hashes"]["reviewed_pipeline"] = file_sha256(Path(__file__))
        self._reviewed_bundle = None

    def reviewed_bundle(self):
        if self._reviewed_bundle is not None:
            return self._reviewed_bundle
        report = json.loads((self.reviewed_root / "report.json").read_text(encoding="utf-8"))
        if report.get("quarantined_parents") or report.get("quarantined_pages"):
            raise ValueError("审计切片仍有隔离项，不得入库")
        checks = report.get("source_evidence_validation", {})
        if checks.get("integrity_errors") or checks.get("answerable_fail"):
            raise ValueError("审计切片未通过完整性或旧题证据检查")
        pages = _records(self.reviewed_root / "pages.jsonl", PageRecord)
        parents = _records(self.reviewed_root / "chunks.jsonl", ChunkRecord)
        children = _records(self.reviewed_root / "embedding_chunks.jsonl", EmbeddingChunk)
        expected = {
            "pages": len(pages),
            "parents": len(parents),
            "children": len(children),
        }
        if any(report.get(key) != value for key, value in expected.items()):
            raise ValueError(f"审计报告数量与实际文件不一致: {expected}")
        if len({child.chunk_id for child in children}) != len(children):
            raise ValueError("审计子片段ID重复")
        if any(page.structure_review_required for page in pages):
            raise ValueError("审计页面仍标记structure_review_required")
        if any(
            child.embedding_model != self.model_manifest["model_id"]
            or child.embedding_revision != self.model_manifest["revision"]
            or child.embedding_token_count > self.token_budget
            for child in children
        ):
            raise ValueError("审计子片段与当前模型或token预算不一致")
        validation = validate_chunks(
            self.reviewed_root / "pages.jsonl",
            self.reviewed_root / "embedding_chunks.jsonl",
            self.root / "eval/dataset.jsonl",
        )
        if validation["integrity_errors"] or validation["answerable_fail"]:
            raise ValueError("审计文件现场复验失败")
        self._reviewed_bundle = pages, parents, children, report
        return self._reviewed_bundle

    def verify_source_pages(self, documents: list[dict]) -> None:
        pages, _, _, _ = self.reviewed_bundle()
        by_document = {}
        for page in pages:
            by_document.setdefault(page.document_id, []).append(page)
        for document in documents:
            expected = by_document.get(document["document_id"], [])
            if len(expected) != document.get("page_count"):
                raise ValueError(f"审计页数与清单不一致: {document['document_id']}")
            actual = list(extract_document(self.root, document))
            if [item.model_dump() for item in actual] != [item.model_dump() for item in expected]:
                raise ValueError(f"审计页面与当前PDF不一致: {document['document_id']}")

    def fingerprint(self, document: dict) -> str:
        pages, parents, children, _ = self.reviewed_bundle()
        document_id = document["document_id"]
        records = [
            item.model_dump()
            for group in (pages, parents, children)
            for item in group
            if item.document_id == document_id
        ]
        return _fingerprint(
            {
                "document": {key: document[key] for key in DOCUMENT_FIELDS},
                "reviewed_records": records,
                "profile": self.profile,
            }
        )

    def process(self, document: dict):
        pages, parents, children, _ = self.reviewed_bundle()
        document_id = document["document_id"]
        selected = tuple(
            [item for item in group if item.document_id == document_id]
            for group in (pages, parents, children)
        )
        if any(not group for group in selected):
            raise ValueError(f"审计产物缺少文档: {document_id}")
        vectors = self.model.encode([child.embedding_text for child in selected[2]])
        validate_vectors(vectors, len(selected[2]), self.model.dimension)
        return *selected, vectors

    def build_candidate(self, verify_sources: bool = True) -> dict:
        with self.lock():
            self.recover()
            documents = self.documents()
            self.reviewed_bundle()
            if verify_sources:
                self.verify_source_pages(documents)
            prepared_ids = {page.document_id for page in self.reviewed_bundle()[0]}
            document_ids = {document["document_id"] for document in documents}
            if prepared_ids != document_ids:
                raise ValueError("审计文档集与清单不一致")
            plans = self.plan(documents, {})
            results, processed_ids, encoded = [], [], 0
            for document, _plan in zip(documents, plans, strict=True):
                result = self.read_cache(document)
                if result is None:
                    result = self.process(document)
                    self.write_cache(document, result)
                    encoded += len(result[2])
                    processed_ids.append(document["document_id"])
                results.append(result)
            state = {
                "profile": self.profile,
                "source": "data/table_audit/prepared",
                "documents": {
                    document["document_id"]: {
                        "fingerprint": self.fingerprint(document),
                        "sha256": document["sha256"],
                    }
                    for document in documents
                },
            }
            staged = self.base / "runs" / uuid4().hex
            staged.mkdir(parents=True)
            pages = [page for result in results for page in result[0]]
            parents = [parent for result in results for parent in result[1]]
            children = [child for result in results for child in result[2]]
            vectors = np.concatenate([result[3] for result in results])
            for relative, records in zip(EXPORTS[:3], (pages, parents, children), strict=True):
                _write_records(staged / relative, records)
            vector_path = staged / EXPORTS[3]
            vector_path.parent.mkdir(parents=True, exist_ok=True)
            validate_vectors(vectors, len(children), vectors.shape[1])
            with vector_path.open("wb") as stream:
                np.savez_compressed(
                    stream,
                    vectors=vectors,
                    chunk_ids=np.asarray([child.chunk_id for child in children]),
                )
            source_hash = file_sha256(staged / EXPORTS[2])
            manifest = {
                "model_id": self.model_manifest["model_id"],
                "revision": self.model_manifest["revision"],
                "weights_sha256": self.weights_hash,
                "signature": _fingerprint(
                    {
                        "source_hash": source_hash,
                        "profile": self.profile,
                        "reviewed_hashes": self.reviewed_hashes,
                    }
                ),
                "source": EXPORTS[2],
                "source_sha256": source_hash,
                "vectors": EXPORTS[3],
                "vectors_sha256": file_sha256(vector_path),
                "normalized": True,
                "dtype": "float32",
                "dimension": vectors.shape[1],
                "model_max_tokens": self.model_limit,
                "token_budget": self.token_budget,
                "max_actual_tokens": max(child.embedding_token_count for child in children),
                "silent_truncation_count": 0,
                "vector_count": len(children),
                "reviewed_bundle_hashes": self.reviewed_hashes,
            }
            _write_json(staged / EXPORTS[4], manifest)
            ingestion = ingest_vectors(staged, self.root / "data/chroma", activate=False)
            _write_json(
                staged / EXPORTS[5],
                {
                    key: value
                    for key, value in ingestion.items()
                    if key not in ("count_before_upsert", "verified_documents", "verified_vectors")
                },
            )
            _write_json(staged / EXPORTS[6], state)
            active_manifest = self.root / "data/chroma/index_manifest.json"
            report = {
                "candidate_built": True,
                "activated": False,
                "run_dir": str(staged),
                "plans": plans,
                "counts": dict(Counter(plan["action"] for plan in plans)),
                "processed_documents": processed_ids,
                "encoded_vectors": encoded,
                "total_documents": len(documents),
                "total_vectors": len(children),
                "collection_name": ingestion["collection_name"],
                "old_index_manifest_sha256": file_sha256(active_manifest),
                "reviewed_bundle_hashes": self.reviewed_hashes,
            }
            _write_json(staged / "candidate_report.json", report)
            _write_json(self.root / "eval/results/reviewed_index_candidate_report.json", report)
            return report

    def _checked_run(self, run_dir: Path) -> Path:
        staged = run_dir.resolve()
        if not staged.is_relative_to((self.base / "runs").resolve()):
            raise ValueError("候选运行目录超出项目范围")
        for relative in EXPORTS:
            if not (staged / relative).is_file():
                raise ValueError(f"候选运行缺少文件: {relative}")
        return staged

    @staticmethod
    def _check_gates(
        old_report: dict, frozen_report: dict, candidate_collection: str
    ) -> dict:
        old = old_report["summary"]
        new = frozen_report["summary"]
        checks = {
            "old_candidate_collection": old_report.get("collection_name")
            == candidate_collection,
            "new_candidate_collection": frozen_report.get("collection_name")
            == candidate_collection,
            "old_hybrid_top5": old_report.get("retrieval_mode") == "hybrid"
            and old_report.get("top_k") == 5,
            "new_hybrid_top5": frozen_report.get("retrieval_mode") == "hybrid"
            and frozen_report.get("top_k") == 5,
            "old_full_dataset": old_report.get("split") is None,
            "new_frozen_split": frozen_report.get("split") == "frozen",
            "old_answerable_18": old["answerable_count"] == 18,
            "old_recall": old["mean_source_page_recall_at_5"] >= OLD_RECALL_GATE,
            "old_all_sources": old["all_sources_hit_count"] >= OLD_ALL_SOURCES_GATE,
            "old_entities": old["entity_correct_count"] == 18,
            "new_answerable_20": new["answerable_count"] == 20,
            "new_recall": new["mean_source_page_recall_at_5"] >= NEW_RECALL_GATE,
            "new_all_sources": new["all_sources_hit_count"] >= NEW_ALL_SOURCES_GATE,
            "new_entities": new["entity_correct_count"] == 20,
            "new_company_coverage": not new.get("companies_with_zero_hits"),
        }
        if not all(checks.values()):
            failed = [name for name, passed in checks.items() if not passed]
            raise ValueError(f"候选索引未通过激活门槛: {failed}")
        return checks

    def activate(
        self, run_dir: Path, old_report_path: Path | None = None,
        frozen_report_path: Path | None = None, *, freeze_path: Path | None = None,
    ) -> dict:
        if freeze_path is None:
            raise ValueError("旧/v2报告不足以授权激活，必须提供独立验收冻结清单")
        from app.retrieval.safe_release import SafeRelease

        return SafeRelease(self.root).activate(run_dir, freeze_path)

    def rollback(self, activation_run: Path) -> dict:
        from app.retrieval.incremental import RETRIEVAL_PROFILE

        if (self.root / RETRIEVAL_PROFILE).exists():
            from app.retrieval.safe_release import SafeRelease

            return SafeRelease(self.root).rollback(activation_run)
        with self.lock():
            self.recover()
            original = self._checked_run(activation_run)
            activation_path = original / "activation.json"
            if not activation_path.is_file():
                raise ValueError("该运行未记录成功激活")
            activation = json.loads(activation_path.read_text(encoding="utf-8"))
            current = json.loads(
                (self.root / "data/chroma/index_manifest.json").read_text(encoding="utf-8")
            )
            if current["collection_name"] != activation["collection_name"]:
                raise ValueError("活动索引已不是该运行的版本，拒绝误回滚")
            rollback_stage = self.base / "runs" / uuid4().hex
            backup = original / "backups"
            for relative in EXPORTS:
                source = backup / relative
                if not source.is_file():
                    raise ValueError(f"回滚备份缺失: {relative}")
                target = rollback_stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            self._publish(rollback_stage)
            report = {
                "rolled_back": True,
                "activation_run": str(original),
                "rollback_run": str(rollback_stage),
                "restored_collection": json.loads(
                    (self.root / "data/chroma/index_manifest.json").read_text(encoding="utf-8")
                )["collection_name"],
            }
            _write_json(rollback_stage / "rollback.json", report)
            return report
