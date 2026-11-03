import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pymupdf
import pytest

from app.retrieval.chroma_store import ChromaIndex
from app.retrieval.incremental import EXPORTS, IncrementalUpdater
from app.retrieval.pdf_extract import file_sha256
from app.retrieval.reviewed_index import ReviewedIndexUpdater


class FakeEncoder:
    dimension = 2
    token_budget = 480
    model = SimpleNamespace(max_seq_length=512)

    def __init__(self, path):
        self.encoded = 0

    def count_tokens(self, text):
        return len(text) + 2

    def encode(self, texts):
        self.encoded += len(texts)
        values = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vector = np.asarray([digest[0] + 1, digest[1] + 1], dtype=np.float32)
            values.append(vector / np.linalg.norm(vector))
        return np.asarray(values)


def write_document(root, document_id, text, code="600519"):
    path = root / "data/raw" / f"{document_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 70), text)
        pdf.save(path)
    return {
        "document_id": document_id,
        "stock_code": code,
        "stock_name": "Test Company",
        "document_type": "annual_report",
        "title": "Test Annual Report",
        "report_period": "2025",
        "publish_date": "2026-04-17",
        "source_url": f"https://example.org/{document_id}.pdf",
        "local_path": f"data/raw/{document_id}.pdf",
        "sha256": file_sha256(path),
        "page_count": 1,
    }


def write_catalog(root, documents):
    path = root / "data/manifests/documents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(document) for document in documents) + "\n", encoding="utf-8"
    )


@pytest.fixture
def project(tmp_path):
    model = tmp_path / "data/models/test-model"
    model.mkdir(parents=True)
    (model / "model.safetensors").write_bytes(b"test-only-weights")
    (model.parent / "embedding_model.json").write_text(
        json.dumps(
            {
                "model_id": "test-model",
                "revision": "revision1",
                "local_path": "data/models/test-model",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval/dataset.jsonl").write_text("", encoding="utf-8")
    documents = [
        write_document(tmp_path, "doc1", "Revenue 2025: 100.00"),
        write_document(tmp_path, "doc2", "Profit 2025: 20.00", "000858"),
    ]
    write_catalog(tmp_path, documents)
    return tmp_path, documents


def updater(root, factory=FakeEncoder):
    return IncrementalUpdater(root, model_factory=factory)


def export_hashes(root):
    return {relative: file_sha256(root / relative) for relative in EXPORTS}


def stage_reviewed_bundle(root):
    target = root / "data/table_audit/prepared"
    target.mkdir(parents=True)
    for relative in EXPORTS[:3]:
        shutil.copy2(root / relative, target / Path(relative).name)
    (target / "quarantine.jsonl").write_text("", encoding="utf-8")
    counts = {
        "pages": len((target / "pages.jsonl").read_text(encoding="utf-8").splitlines()),
        "parents": len((target / "chunks.jsonl").read_text(encoding="utf-8").splitlines()),
        "children": len(
            (target / "embedding_chunks.jsonl").read_text(encoding="utf-8").splitlines()
        ),
    }
    (target / "report.json").write_text(
        json.dumps(
            {
                **counts,
                "quarantined_parents": 0,
                "quarantined_pages": 0,
                "source_evidence_validation": {"integrity_errors": [], "answerable_fail": 0},
            }
        ),
        encoding="utf-8",
    )


def test_plan_does_not_write_or_load_model(project):
    root, _ = project
    report = updater(root, lambda _: pytest.fail("计划不能加载模型")).update(dry_run=True)
    assert report["counts"] == {"new": 2}
    assert not (root / "data/incremental").exists()


def test_unchanged_documents_reuse_without_model_or_index_write(project):
    root, _ = project
    first = updater(root).update()
    assert first["counts"] == {"new": 2}
    assert first["encoded_vectors"] > 0
    before = export_hashes(root)
    report = updater(root, lambda _: pytest.fail("未变化文档不能重新加载模型")).update()
    assert report["counts"] == {"reuse": 2}
    assert report["encoded_vectors"] == 0
    assert report["index_published"] is False
    assert export_hashes(root) == before


def test_new_and_changed_documents_only_encode_their_own_chunks(project):
    root, documents = project
    updater(root).update()
    original_ids = set(ChromaIndex(root / "data/chroma").collection.get()["ids"])
    third = write_document(root, "doc3", "New report 2025: 50.00", "000568")
    write_catalog(root, documents + [third])
    added = updater(root).update()
    assert added["processed_documents"] == ["doc3"]
    assert added["counts"] == {"reuse": 2, "new": 1}
    added_ids = set(ChromaIndex(root / "data/chroma").collection.get()["ids"])
    assert original_ids < added_ids
    assert added["encoded_vectors"] == len(added_ids - original_ids)
    changed = write_document(root, "doc1", "Revenue 2025: 200.00")
    write_catalog(root, [changed, documents[1], third])
    updated = updater(root).update()
    assert updated["processed_documents"] == ["doc1"]
    assert updated["counts"] == {"changed_or_cache_invalid": 1, "reuse": 2}
    index = ChromaIndex(root / "data/chroma")
    stored = index.collection.get(where={"document_id": "doc1"})
    assert all("200.00" in text for text in stored["documents"])
    assert index.collection.count() == len(added_ids)


def test_bad_manifest_hash_and_removed_documents_preserve_active_index(project):
    root, documents = project
    updater(root).update()
    before = export_hashes(root)
    write_document(root, "doc1", "Changed without updating catalog")
    with pytest.raises(ValueError, match="哈希与清单不一致"):
        updater(root).update()
    assert export_hashes(root) == before
    write_catalog(root, [documents[1]])
    with pytest.raises(ValueError, match="不自动删除"):
        updater(root).update()
    assert export_hashes(root) == before


def test_corrupt_cache_rebuilds_only_affected_document(project):
    root, documents = project
    updater(root).update()
    path = updater(root).cache_path(documents[0]) / "chunks.jsonl"
    path.write_text("broken", encoding="utf-8")
    report = updater(root).update()
    assert report["processed_documents"] == ["doc1"]
    assert report["counts"] == {"changed_or_cache_invalid": 1, "reuse": 1}


def test_model_failure_preserves_previous_exports(project):
    root, documents = project
    updater(root).update()
    before = export_hashes(root)
    documents[0] = write_document(root, "doc1", "Revenue updated 2025: 300.00")
    write_catalog(root, documents)

    class BrokenEncoder(FakeEncoder):
        def encode(self, texts):
            raise RuntimeError("simulated encoding failure")

    with pytest.raises(RuntimeError, match="encoding failure"):
        updater(root, BrokenEncoder).update()
    assert export_hashes(root) == before
    # 锁文件可以保留，关键是失败后OS锁已经释放，下一次能够继续更新。
    assert updater(root).update()["index_published"] is True


def test_publish_failure_rolls_back_and_retry_publishes_cached_change(project, monkeypatch):
    import app.retrieval.incremental as implementation

    root, documents = project
    updater(root).update()
    before = export_hashes(root)
    documents[0] = write_document(root, "doc1", "Revenue 2025: 900.00")
    write_catalog(root, documents)
    copy = implementation.shutil.copy2

    def fail_during_publish(source, destination, *args, **kwargs):
        if str(destination).endswith(".publish.tmp"):
            raise OSError("simulated publish failure")
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(implementation.shutil, "copy2", fail_during_publish)
    with pytest.raises(OSError, match="publish failure"):
        updater(root).update()
    assert export_hashes(root) == before
    monkeypatch.setattr(implementation.shutil, "copy2", copy)
    retried = updater(root, lambda _: pytest.fail("重试应复用已验证的新缓存")).update()
    assert retried["encoded_vectors"] == 0
    assert retried["index_published"] is True
    assert (
        "900.00"
        in ChromaIndex(root / "data/chroma").collection.get(where={"document_id": "doc1"})[
            "documents"
        ][0]
    )


def test_processing_profile_change_invalidates_cache(project):
    root, _ = project
    updater(root).update()
    changed = updater(root)
    changed.profile["pipeline_version"] = "changed-test-version"
    report = changed.update()
    assert report["counts"] == {"changed_or_cache_invalid": 2}
    assert len(report["processed_documents"]) == 2


def test_verified_legacy_outputs_can_bootstrap_without_reembedding(project, monkeypatch):
    root, _ = project
    updater(root).update()
    state_path = root / "data/incremental/state.json"
    assert state_path.resolve().is_relative_to(root.resolve())
    state_path.unlink()
    migrated = updater(root, lambda _: pytest.fail("迁移旧产物不应重新向量化"))
    monkeypatch.setattr(migrated, "read_cache", lambda _: None)
    report = migrated.update()
    assert report["counts"] == {"adopt_existing": 2}
    assert report["encoded_vectors"] == 0
    assert report["index_published"] is False


def test_os_lock_blocks_concurrent_writer_and_releases_after_exit(project):
    root, _ = project
    first, second = updater(root), updater(root)
    with first.lock():
        with pytest.raises(RuntimeError, match="已有进程"):
            with second.lock():
                pytest.fail("第二个写者不应进入更新区间")
    with second.lock():
        pass


def test_interrupted_publish_journal_is_recovered_before_update(project):
    import shutil

    from app.retrieval.incremental import _write_json

    root, _ = project
    updater(root).update()
    before = export_hashes(root)
    staged = root / "data/incremental/runs/interruption-test"
    for relative in EXPORTS:
        backup = staged / "backups" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, backup)
    _write_json(
        root / "data/incremental/pending_publish.json",
        {
            "run_dir": str(staged),
            "existing": dict.fromkeys(EXPORTS, True),
        },
    )
    (root / EXPORTS[0]).write_text("partial publication", encoding="utf-8")
    report = updater(root, lambda _: pytest.fail("恢复后无需重算")).update()
    assert report["encoded_vectors"] == 0
    assert export_hashes(root) == before
    assert not (root / "data/incremental/pending_publish.json").exists()


def test_reviewed_candidate_rejects_legacy_activation_without_independent_gate(project, tmp_path):
    root, _ = project
    updater(root).update()
    old_collection = ChromaIndex(root / "data/chroma").manifest["collection_name"]
    stage_reviewed_bundle(root)
    report = ReviewedIndexUpdater(root, model_factory=FakeEncoder).build_candidate()
    candidate_collection = report["collection_name"]
    assert candidate_collection != old_collection
    assert ChromaIndex(root / "data/chroma").manifest["collection_name"] == old_collection

    old_report = tmp_path / "old.json"
    frozen_report = tmp_path / "frozen.json"
    old_report.write_text(
        json.dumps(
            {
                "collection_name": candidate_collection,
                "retrieval_mode": "hybrid",
                "top_k": 5,
                "split": None,
                "summary": {
                    "answerable_count": 18,
                    "mean_source_page_recall_at_5": 0.8,
                    "all_sources_hit_count": 13,
                    "entity_correct_count": 18,
                }
            }
        ),
        encoding="utf-8",
    )
    frozen_report.write_text(
        json.dumps(
            {
                "collection_name": candidate_collection,
                "retrieval_mode": "hybrid",
                "top_k": 5,
                "split": "frozen",
                "summary": {
                    "answerable_count": 20,
                    "mean_source_page_recall_at_5": 0.9,
                    "all_sources_hit_count": 16,
                    "entity_correct_count": 20,
                    "companies_with_zero_hits": [],
                }
            }
        ),
        encoding="utf-8",
    )
    reviewed = ReviewedIndexUpdater(root, model_factory=FakeEncoder)
    with pytest.raises(ValueError, match="独立验收冻结清单"):
        reviewed.activate(Path(report["run_dir"]), old_report, frozen_report)
    assert ChromaIndex(root / "data/chroma").manifest["collection_name"] == old_collection
