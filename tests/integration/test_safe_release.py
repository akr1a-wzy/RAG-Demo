import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.retrieval.incremental import EXPORTS, RETRIEVAL_PROFILE, IncrementalUpdater, _write_json
from app.retrieval.pdf_extract import file_sha256
from app.retrieval.published import (
    VALIDATED_PARAMETERS,
    check_holdout,
    open_published_retriever,
    validate_profile,
)
from app.retrieval.reviewed_index import ReviewedIndexUpdater


def transaction_workspace(root):
    updater = object.__new__(IncrementalUpdater)
    updater.root, updater.base = root, root / "data/incremental"
    stage = updater.base / "runs/test-release"
    for relative in (*EXPORTS, RETRIEVAL_PROFILE):
        for base, prefix in ((root, "old:"), (stage, "new:")):
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(prefix + relative, encoding="utf-8")
    return updater, stage


def test_post_publication_failure_restores_index_and_profile_byte_for_byte(tmp_path):
    updater, stage = transaction_workspace(tmp_path)
    paths = (*EXPORTS, RETRIEVAL_PROFILE)
    before = {p: file_sha256(tmp_path / p) for p in paths}

    def fail_after_all_files_switched():
        assert all((tmp_path / p).read_text(encoding="utf-8").startswith("new:") for p in paths)
        raise RuntimeError("post-publication-query-failed")

    with pytest.raises(RuntimeError, match="post-publication-query-failed"):
        updater._publish(stage, (RETRIEVAL_PROFILE,), validate=fail_after_all_files_switched)
    assert {p: file_sha256(tmp_path / p) for p in paths} == before
    assert not (updater.base / "pending_publish.json").exists()


def test_interrupted_transaction_recovers_profile_as_well_as_index(tmp_path):
    updater, stage = transaction_workspace(tmp_path)
    paths = (*EXPORTS, RETRIEVAL_PROFILE)
    for p in paths:
        backup = stage / "backups" / p
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes((tmp_path / p).read_bytes())
        (tmp_path / p).write_text("interrupted", encoding="utf-8")
    _write_json(updater.base / "pending_publish.json",
                {"run_dir": str(stage), "existing": dict.fromkeys(paths, True)})
    updater.recover()
    assert all((tmp_path / p).read_text(encoding="utf-8").startswith("old:") for p in paths)


def test_protected_workspace_rejects_ungated_update_and_partial_publish(tmp_path):
    updater, stage = transaction_workspace(tmp_path)
    with pytest.raises(ValueError, match="验收门槛"):
        updater.update()
    with pytest.raises(ValueError, match="同时发布"):
        updater._publish(stage)


def test_legacy_reviewed_reports_cannot_authorize_activation(tmp_path):
    updater = object.__new__(ReviewedIndexUpdater)
    with pytest.raises(ValueError, match="独立验收冻结清单"):
        updater.activate(tmp_path, tmp_path / "old.json", tmp_path / "v2.json")


def profile_workspace(root):
    artifact = root / "snapshot"
    _write_json(artifact / EXPORTS[5], {"collection_name": "validated-collection"})
    artifact_hash = {"snapshot/" + EXPORTS[5]: file_sha256(artifact / EXPORTS[5])}
    rules = root / "config/rules.json"
    _write_json(rules, {"rule": "original"})
    proof = {
        "release_id": "release1", "gate_passed": True,
        "collection_name": "validated-collection", "parameters": VALIDATED_PARAMETERS,
        "artifact_hashes": artifact_hash,
        "runtime_hashes": {"config/rules.json": file_sha256(rules)},
    }
    _write_json(root / "proof.json", proof)
    return {
        "release_id": "release1", "kind": "validated", "device": "cuda",
        "artifact_root": "snapshot", "collection_name": "validated-collection",
        "parameters": dict(VALIDATED_PARAMETERS), "artifact_hashes": artifact_hash,
        "proof": {"path": "proof.json", "sha256": file_sha256(root / "proof.json")},
    }


def test_published_profile_rejects_runtime_config_change(tmp_path):
    profile = profile_workspace(tmp_path)
    assert validate_profile(tmp_path, profile) == tmp_path / "snapshot"
    _write_json(tmp_path / "config/rules.json", {"rule": "changed"})
    with pytest.raises(ValueError, match="已验收版本不一致"):
        validate_profile(tmp_path, profile)


def test_published_profile_cannot_silently_disable_reranking(tmp_path):
    profile = profile_workspace(tmp_path)
    profile["parameters"]["reranker"] = False
    with pytest.raises(ValueError, match="已验收策略"):
        validate_profile(tmp_path, profile)


def test_default_query_rejects_pending_publication(tmp_path):
    profile = profile_workspace(tmp_path)
    _write_json(tmp_path / "data/incremental/pending_publish.json", {})
    with pytest.raises(RuntimeError, match="等待恢复"):
        validate_profile(tmp_path, profile)


def test_gpu_unavailable_fails_before_model_loading_and_restores_environment(tmp_path, monkeypatch):
    profile = profile_workspace(tmp_path)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False)
    ))
    monkeypatch.setenv("RAG_DEVICE", "cpu")
    with pytest.raises(RuntimeError, match="默认检索要求GPU"):
        open_published_retriever(tmp_path, profile=profile)
    import os

    assert os.environ["RAG_DEVICE"] == "cpu"


@pytest.mark.parametrize("option", [["--top-k", "10"], ["--mode", "vector"], ["--device", "cpu"]])
def test_cli_requires_explicit_diagnostic_mode_for_overrides(monkeypatch, option):
    script = Path(__file__).resolve().parents[2] / "scripts/search_documents.py"
    spec = importlib.util.spec_from_file_location("search_documents_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(script), "问题", *option])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_high_mean_recall_cannot_hide_incomplete_evidence_gate():
    report = {"top_k": 5, "summary": {
        "answerable_count": 30, "refusal_not_evaluated": 5,
        "mean_evidence_group_recall_at_5": 0.99, "all_sources_hit_count": 23,
        "answer_fields_complete_count": 26, "entity_correct_count": 30,
        "company_coverage": {str(n): {} for n in range(20)}, "companies_with_zero_hits": [],
    }}
    assert not all(check_holdout(report).values())
