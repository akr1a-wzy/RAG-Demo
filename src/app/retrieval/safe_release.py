"""绑定一次独立验收与不可变语料快照，事务发布索引和查询策略。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.retrieval.chroma_store import ChromaIndex
from app.retrieval.incremental import EXPORTS, RETRIEVAL_PROFILE, IncrementalUpdater, _write_json
from app.retrieval.pdf_extract import file_sha256
from app.retrieval.published import (
    VALIDATED_PARAMETERS,
    check_hashes,
    check_holdout,
    open_published_retriever,
    project_path,
    read_json,
)


def copy_exports(source: Path, target: Path, exports=EXPORTS) -> None:
    for relative in exports:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)


def artifact_hashes(root: Path, artifact: Path) -> dict[str, str]:
    return {(artifact / relative).relative_to(root).as_posix(): file_sha256(artifact / relative)
            for relative in EXPORTS}


def companies(artifact: Path) -> list[str]:
    return sorted({json.loads(line)["stock_code"]
                   for line in (artifact / EXPORTS[2]).read_text(encoding="utf-8").splitlines()
                   if line.strip()})


def verify_equivalence(root: Path, profile: dict, reports: list[dict], *, published=False) -> dict:
    start = perf_counter()
    runtime = open_published_retriever(
        root, profile=None if published else profile, during_publish=published,
    )
    checks = []
    for reference in reports:
        report = read_json(project_path(root, reference["report"]))
        for case in report["cases"]:
            if "hits" not in case:
                continue
            hits, _ = runtime.retrieve_resolved(case["resolved"])
            actual = [(h.document_id, h.page_start, h.chunk_id) for h in hits]
            expected = [(h["document_id"], h["page"], h["chunk_id"]) for h in case["hits"]]
            checks.append({"dataset_role": reference["dataset_role"], "case_id": case["id"],
                           "identical_top5": actual == expected})
    return {"passed": bool(checks) and all(c["identical_top5"] for c in checks),
            "cases": checks, "case_count": len(checks), "runtime": runtime.metadata(),
            "seconds": perf_counter() - start,
            "scope": "Known regression page/chunk/order equivalence, not a new blind evaluation"}


class SafeRelease:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.updater = IncrementalUpdater(self.root)

    def _bootstrap_legacy(self) -> None:
        if (self.root / RETRIEVAL_PROFILE).exists():
            return
        legacy = self.updater.base / "runs" / ("legacy-" + uuid4().hex)
        copy_exports(self.root, legacy)
        manifest = read_json(legacy / EXPORTS[5])
        codes = companies(legacy)
        profile = {
            "release_id": legacy.name, "kind": "legacy_rollback", "device": "cuda",
            "artifact_root": legacy.relative_to(self.root).as_posix(),
            "artifact_hashes": artifact_hashes(self.root, legacy),
            "collection_name": manifest["collection_name"], "companies": codes,
            "parameters": {"mode": "hybrid", "top_k": 5, "candidate_k": 20,
                           "reranker": False, "task_aware": False, "page_dedup": False},
            "coverage_notice": f"旧版回退配置，仅覆盖{len(codes)}家公司；不代表新独立验收策略。",
        }
        _write_json(legacy / RETRIEVAL_PROFILE, profile)
        _write_json(self.root / RETRIEVAL_PROFILE, profile)

    def _proof(self, candidate: Path, freeze_path: Path, stage: Path) -> tuple[dict, dict]:
        freeze = read_json(freeze_path)
        if freeze["parameters"] != VALIDATED_PARAMETERS:
            raise ValueError("冻结参数不是本次已确认的默认检索策略")
        if project_path(self.root, freeze["candidate_root"]) != candidate:
            raise ValueError("候选目录与独立验收版本不同")
        check_hashes(self.root, freeze["file_hashes"])
        dataset = project_path(self.root, freeze["blind_manifest"]["dataset"])
        if file_sha256(dataset) != freeze["blind_manifest"]["sha256"]:
            raise ValueError("独立验收数据集哈希变化")
        state_path = dataset.with_name(dataset.stem + "_evaluation_state.json")
        state = read_json(state_path)
        report_path = project_path(self.root, state["report"].replace("\\", "/"))
        report = read_json(report_path)
        manifest = read_json(candidate / EXPORTS[5])
        checks = check_holdout(report)
        checks.update({
            "evaluated_once": state["status"] == "evaluated_once" and state["attempt_count"] == 1,
            "state_passed": state.get("gate_passed") is True,
            "freeze_bound": state["freeze_sha256"] == file_sha256(freeze_path),
            "report_bound": state["report_sha256"] == file_sha256(report_path),
            "dataset_bound": state["dataset_sha256"] == file_sha256(dataset)
            == report["dataset_sha256"],
            "state_summary_bound": state["summary"] == report["summary"],
            "collection_bound": report["collection_name"] == manifest["collection_name"],
            "pipeline_bound": report["pipeline_version"] == freeze["pipeline"],
            "cuda_bound": report["embedding_device"] == report["reranker_device"] == "cuda",
            "not_tuned_on_holdout": (
                state.get("primary_agent_read_case_content_for_tuning") is False
            ),
            "known_selection_passed": all(freeze["selection_gate_checks"].values()),
        })
        for key, value in VALIDATED_PARAMETERS.items():
            report_key = "retrieval_mode" if key == "mode" else key
            checks[f"parameter_{key}"] = report.get(report_key) == value
        if not all(checks.values()):
            raise ValueError(f"独立验收绑定失败: {[k for k, v in checks.items() if not v]}")
        # 重算旧集/v2硬门槛，而非仅信任冻结清单中的布尔值。
        from app.retrieval.reviewed_index import ReviewedIndexUpdater

        references = {r["dataset_role"]: r for r in freeze["reports"]}
        ReviewedIndexUpdater._check_gates(
            read_json(project_path(self.root, references["old"]["report"])),
            read_json(project_path(self.root, references["v2_regression"]["report"])),
            manifest["collection_name"],
        )
        index = ChromaIndex(self.root / "data/chroma", artifact_root=candidate)
        if index.manifest["source_sha256"] != file_sha256(candidate / EXPORTS[2]):
            raise ValueError("候选切片哈希不符")
        if index.manifest["vectors_sha256"] != file_sha256(candidate / EXPORTS[3]):
            raise ValueError("候选向量哈希不符")
        codes = companies(candidate)
        if len(codes) != 20:
            raise ValueError("候选不是已验收的20家公司")
        runtime_hashes = {p: h for p, h in freeze["file_hashes"].items()
                          if p != EXPORTS[5]}
        # 活动manifest在发布时必然变化；原冻结清单保留，不能事后改它。
        for path in (freeze_path, state_path, report_path,
                     self.root / "src/app/retrieval/published.py",
                     self.root / "src/app/retrieval/safe_release.py",
                     self.root / "src/app/retrieval/incremental.py",
                     self.root / "scripts/search_documents.py"):
            runtime_hashes[path.relative_to(self.root).as_posix()] = file_sha256(path)
        proof = {
            "release_id": stage.name,
            "gate_passed": True, "gate_checks": checks,
            "parameters": freeze["parameters"], "collection_name": manifest["collection_name"],
            "artifact_hashes": artifact_hashes(self.root, candidate),
            "runtime_hashes": runtime_hashes, "freeze_sha256": file_sha256(freeze_path),
            "pre_publication_active_manifest_sha256": file_sha256(self.root / EXPORTS[5]),
            "mutable_publication_exports": [*EXPORTS, RETRIEVAL_PROFILE],
        }
        _write_json(stage / "gate_proof.json", proof)
        profile = {
            "release_id": stage.name, "kind": "validated", "device": "cuda",
            "artifact_root": candidate.relative_to(self.root).as_posix(),
            "artifact_hashes": proof["artifact_hashes"],
            "collection_name": manifest["collection_name"],
            "companies": codes, "parameters": freeze["parameters"],
            "proof": {"path": (stage / "gate_proof.json").relative_to(self.root).as_posix(),
                      "sha256": file_sha256(stage / "gate_proof.json")},
            "coverage_notice": "20家公司文档检索；生成和拒答尚未验收。",
        }
        return profile, freeze

    def activate(self, candidate: Path, freeze_path: Path, *, rehearse_rollback=False) -> dict:
        candidate, freeze_path = candidate.resolve(), freeze_path.resolve()
        if not candidate.is_relative_to(self.updater.base / "runs"):
            raise ValueError("候选必须位于本项目运行目录")
        with self.updater.lock():
            self.updater.recover()
            stage = self.updater.base / "runs" / ("release-" + uuid4().hex)
            stage.mkdir(parents=True)
            profile, freeze = self._proof(candidate, freeze_path, stage)
            self._bootstrap_legacy()
            exports = (*EXPORTS, RETRIEVAL_PROFILE)
            old_hashes = {p: file_sha256(self.root / p) for p in exports}
            copy_exports(candidate, stage)
            _write_json(stage / RETRIEVAL_PROFILE, profile)
            pre = verify_equivalence(self.root, profile, freeze["reports"])
            _write_json(stage / "pre_equivalence.json", pre)
            if not pre["passed"]:
                raise ValueError(f"统一入口与已验收已见题不一致；证据保留于{stage}")

            def validate():
                post = verify_equivalence(self.root, profile, freeze["reports"], published=True)
                _write_json(stage / "post_equivalence.json", post)
                if not post["passed"]:
                    raise ValueError("切换后检索不一致")
                if rehearse_rollback:
                    raise RuntimeError("intentional-post-publication-rollback-drill")

            try:
                self.updater._publish(stage, (RETRIEVAL_PROFILE,), validate=validate)
            except BaseException as error:
                restored = {p: file_sha256(self.root / p) for p in exports}
                result = {"activated": False, "rolled_back": restored == old_hashes,
                          "rehearsal": rehearse_rollback, "error": str(error),
                          "run_dir": str(stage), "old_hashes": old_hashes,
                          "restored_hashes": restored,
                          "coverage_notice": read_json(
                              self.root / RETRIEVAL_PROFILE
                          )["coverage_notice"]}
                _write_json(stage / "activation_failed.json", result)
                _write_json(self.root / "eval/results/safe_activation_failed.json", result)
                if (rehearse_rollback
                        and str(error) == "intentional-post-publication-rollback-drill"):
                    if not result["rolled_back"]:
                        raise RuntimeError("回退演练未恢复全部产物") from error
                    return result
                raise
            result = {
                "activated": True, "run_dir": str(stage), "release_id": stage.name,
                "candidate_root": str(candidate), "collection_name": profile["collection_name"],
                "profile_sha256": file_sha256(self.root / RETRIEVAL_PROFILE),
                "artifact_hashes": {p: file_sha256(self.root / p) for p in exports},
                "gate_proof_sha256": file_sha256(stage / "gate_proof.json"),
                "rollback_backup": str(stage / "backups"),
                "known_equivalence_cases": pre["case_count"], "blind_rerun": False,
            }
            _write_json(stage / "activation.json", result)
            _write_json(self.root / "eval/results/safe_activation.json", result)
            return result

    def rollback(self, activation_run: Path) -> dict:
        original = activation_run.resolve()
        if not original.is_relative_to(self.updater.base / "runs"):
            raise ValueError("回退路径超出项目")
        with self.updater.lock():
            self.updater.recover()
            activation = read_json(original / "activation.json")
            current = read_json(self.root / RETRIEVAL_PROFILE)
            if current["release_id"] != activation["release_id"]:
                raise ValueError("当前不是该发布版本，拒绝误回退")
            stage = self.updater.base / "runs" / ("rollback-" + uuid4().hex)
            exports = (*EXPORTS, RETRIEVAL_PROFILE)
            copy_exports(original / "backups", stage, exports)
            self.updater._publish(stage, (RETRIEVAL_PROFILE,))
            restored = read_json(self.root / RETRIEVAL_PROFILE)
            result = {"rolled_back": True, "run_dir": str(stage),
                      "restored_collection": restored["collection_name"],
                      "coverage_notice": restored["coverage_notice"]}
            _write_json(stage / "rollback.json", result)
            return result
