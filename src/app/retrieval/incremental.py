"""文档级增量计算与可恢复的版本快照发布。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import numpy as np

from app.retrieval import chunking, embedding_prepare, embeddings, pdf_extract
from app.retrieval.chroma_store import INDEX_VERSION, ingest_vectors
from app.retrieval.chunking import ChunkConfig, ChunkRecord, split_page, validate_chunks
from app.retrieval.embedding_prepare import EmbeddingChunk, load_confirmed_headers, prepare_chunk
from app.retrieval.embeddings import LocalEmbeddingModel, validate_vectors
from app.retrieval.pdf_extract import PageRecord, extract_document, file_sha256, load_manifest

DOCUMENT_FIELDS = (
    "document_id",
    "stock_code",
    "stock_name",
    "document_type",
    "title",
    "report_period",
    "publish_date",
    "source_url",
    "local_path",
    "sha256",
)
EXPORTS = (
    "data/processed/pages.jsonl",
    "data/processed/chunks.jsonl",
    "data/processed/embedding_chunks.jsonl",
    "data/embeddings/vectors.npz",
    "data/embeddings/manifest.json",
    "data/chroma/index_manifest.json",
    "data/incremental/state.json",
)
RETRIEVAL_PROFILE = "data/retrieval/active_profile.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _records(path: Path, record_type):
    return [
        record_type.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_records(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8"
    )


class IncrementalUpdater:
    def __init__(self, root: Path, model_factory: Callable | None = None) -> None:
        self.root = root.resolve()
        self.base = self.root / "data/incremental"
        self.model_manifest = _json(self.root / "data/models/embedding_model.json")
        self.model_path = self.root / self.model_manifest["local_path"]
        self.weights_hash = file_sha256(self.model_path / "model.safetensors")
        config = (
            _json(self.model_path / "config.json")
            if (self.model_path / "config.json").exists()
            else {}
        )
        sentence = (
            _json(self.model_path / "sentence_bert_config.json")
            if (self.model_path / "sentence_bert_config.json").exists()
            else {}
        )
        self.model_limit = min(
            config.get("max_position_embeddings", 512), sentence.get("max_seq_length", 512)
        )
        self.token_budget = min(480, self.model_limit)
        self.model_factory = model_factory or LocalEmbeddingModel
        self._model = None
        # 代码与依赖也纳入指纹，防止改了切片规则却错误复用旧结果。
        self.profile = {
            "pipeline_version": "document-cache-v1",
            "index_version": INDEX_VERSION,
            "chunking_version": chunking.CHUNKING_VERSION,
            "preparation_version": embedding_prepare.PREPARATION_VERSION,
            "chunk_size": 800,
            "chunk_overlap": 100,
            "token_budget": self.token_budget,
            "model_id": self.model_manifest["model_id"],
            "revision": self.model_manifest["revision"],
            "weights_sha256": self.weights_hash,
            "model_config_hashes": {
                relative: file_sha256(self.model_path / relative)
                for relative in (
                    "config.json",
                    "sentence_bert_config.json",
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "modules.json",
                    "1_Pooling/config.json",
                )
                if (self.model_path / relative).is_file()
            },
            "code_hashes": {
                name: file_sha256(Path(module.__file__))
                for name, module in (
                    ("extract", pdf_extract),
                    ("chunk", chunking),
                    ("prepare", embedding_prepare),
                    ("embed", embeddings),
                )
            },
            "dependencies": {
                name: version(name)
                for name in (
                    "pymupdf",
                    "pypdf",
                    "langchain-text-splitters",
                    "sentence-transformers",
                    "transformers",
                )
            },
        }
        self.profile["code_hashes"]["pipeline"] = file_sha256(Path(__file__))

    @property
    def model(self):
        if self._model is None:
            self._model = self.model_factory(self.model_path)
        return self._model

    def documents(self) -> list[dict]:
        documents = load_manifest(self.root / "data/manifests/documents.jsonl")
        if not documents:
            raise ValueError("文档清单不能为空")
        seen = set()
        for document in documents:
            if document["document_id"] in seen:
                raise ValueError(f"重复document_id: {document['document_id']}")
            seen.add(document["document_id"])
            path = (self.root / document["local_path"]).resolve()
            if not path.is_relative_to(self.root / "data/raw"):
                raise ValueError("原始文档必须位于项目data/raw中")
            if file_sha256(path) != document["sha256"]:
                raise ValueError(
                    f"PDF哈希与清单不一致: {document['document_id']}；"
                    "确认新文件并更新documents.jsonl的sha256、页数后再运行"
                )
        return documents

    def fingerprint(self, document: dict) -> str:
        return _fingerprint(
            {
                "document": {key: document[key] for key in DOCUMENT_FIELDS},
                "page_review": self.page_review_fingerprint(document),
                "page_count": document.get("page_count"),
                "profile": self.profile,
            }
        )

    def page_review_fingerprint(self, document: dict):
        reviews = {}
        for name in ("pdf_page_reviews.json", "table_header_reviews.json"):
            path = self.root / "data/manifests" / name
            if path.exists():
                reviews[name] = [
                    r for r in _json(path)["reviews"] if r["document_id"] == document["document_id"]
                ]
        return _fingerprint(reviews) if reviews else None

    def cache_path(self, document: dict) -> Path:
        return self.base / "documents" / self.fingerprint(document)

    def read_cache(self, document: dict):
        path = self.cache_path(document)
        try:
            manifest = _json(path / "cache_manifest.json")
            if manifest["fingerprint"] != self.fingerprint(document):
                return None
            for filename, checksum in manifest["files"].items():
                if file_sha256(path / filename) != checksum:
                    return None
            pages = _records(path / "pages.jsonl", PageRecord)
            parents = _records(path / "chunks.jsonl", ChunkRecord)
            children = _records(path / "embedding_chunks.jsonl", EmbeddingChunk)
            with np.load(path / "vectors.npz", allow_pickle=False) as archive:
                vectors, ids = archive["vectors"], archive["chunk_ids"].tolist()
            if ids != [child.chunk_id for child in children]:
                return None
            if any(page.document_id != document["document_id"] for page in pages):
                return None
            validate_vectors(vectors, len(children), manifest["dimension"])
            return pages, parents, children, vectors
        except (OSError, ValueError, KeyError):
            return None

    def write_cache(self, document: dict, result) -> None:
        pages, parents, children, vectors = result
        path = self.cache_path(document)
        path.mkdir(parents=True, exist_ok=True)
        _write_records(path / "pages.jsonl", pages)
        _write_records(path / "chunks.jsonl", parents)
        _write_records(path / "embedding_chunks.jsonl", children)
        with (path / "vectors.npz").open("wb") as stream:
            np.savez_compressed(
                stream,
                vectors=vectors,
                chunk_ids=np.asarray([child.chunk_id for child in children]),
            )
        _write_json(
            path / "cache_manifest.json",
            {
                "document_id": document["document_id"],
                "fingerprint": self.fingerprint(document),
                "dimension": vectors.shape[1],
                "profile": self.profile,
                "files": {
                    filename: file_sha256(path / filename)
                    for filename in (
                        "pages.jsonl",
                        "chunks.jsonl",
                        "embedding_chunks.jsonl",
                        "vectors.npz",
                    )
                },
            },
        )

    def baseline(self, documents: list[dict]) -> dict:
        """认证现有全量产物，首次迁移时无需重复Embedding。"""
        state_path = self.base / "state.json"
        if state_path.exists():
            return {}
        manifest_path = self.root / "data/embeddings/manifest.json"
        if not manifest_path.exists():
            return {}
        manifest = _json(manifest_path)
        if (manifest["model_id"], manifest["revision"], manifest["weights_sha256"]) != (
            self.model_manifest["model_id"],
            self.model_manifest["revision"],
            self.weights_hash,
        ):
            return {}
        child_path, vector_path = self.root / manifest["source"], self.root / manifest["vectors"]
        if file_sha256(child_path) != manifest["source_sha256"]:
            return {}
        if file_sha256(vector_path) != manifest["vectors_sha256"]:
            return {}
        pages = _records(self.root / "data/processed/pages.jsonl", PageRecord)
        parents = _records(self.root / "data/processed/chunks.jsonl", ChunkRecord)
        children = _records(child_path, EmbeddingChunk)
        with np.load(vector_path, allow_pickle=False) as archive:
            vectors, ids = archive["vectors"], archive["chunk_ids"].tolist()
        if ids != [child.chunk_id for child in children]:
            return {}
        validate_vectors(vectors, len(children), manifest["dimension"])
        positions = {chunk_id: i for i, chunk_id in enumerate(ids)}
        baseline = {}
        # 认证原页面来自同一文档信息，正文切片来自原页面，模型与参数匹配。
        for document in documents:
            document_id = document["document_id"]
            doc_pages = [page for page in pages if page.document_id == document_id]
            doc_parents = [parent for parent in parents if parent.document_id == document_id]
            doc_children = [child for child in children if child.document_id == document_id]
            if not doc_pages or not doc_parents or not doc_children:
                continue
            if any(
                getattr(page, key) != document[key]
                for page in doc_pages
                for key in (
                    "stock_code",
                    "stock_name",
                    "document_type",
                    "title",
                    "report_period",
                    "publish_date",
                    "source_url",
                    "local_path",
                )
            ):
                continue
            if document.get("page_count") != len(doc_pages):
                continue
            # 首次迁移做一次低成本提取核对，防止把已替换PDF错误认作旧产物。
            verified_pages = list(extract_document(self.root, document))
            if [page.model_dump() for page in verified_pages] != [
                page.model_dump() for page in doc_pages
            ]:
                continue
            if any(
                parent.chunking_version != self.profile["chunking_version"]
                or parent.chunk_size != 800
                or parent.chunk_overlap != 100
                for parent in doc_parents
            ):
                continue
            if any(
                child.preparation_version != self.profile["preparation_version"]
                or child.embedding_model != self.model_manifest["model_id"]
                or child.embedding_revision != self.model_manifest["revision"]
                or child.token_budget != self.token_budget
                for child in doc_children
            ):
                continue
            page_map = {page.page_number: page for page in doc_pages}
            if any(
                page_map[child.page_start].text[child.start_char : child.end_char] != child.text
                for child in doc_children
            ):
                continue
            baseline[document_id] = (
                doc_pages,
                doc_parents,
                doc_children,
                np.asarray([vectors[positions[child.chunk_id]] for child in doc_children]),
            )
        return baseline

    def plan(self, documents: list[dict], baseline: dict) -> list[dict]:
        state = _json(self.base / "state.json") if (self.base / "state.json").exists() else {}
        old_documents = state.get("documents", {})
        removed = set(old_documents) - {document["document_id"] for document in documents}
        if removed:
            raise ValueError(f"清单移除了文档{sorted(removed)}；本阶段不自动删除旧知识")
        plans = []
        for document in documents:
            document_id = document["document_id"]
            if self.read_cache(document) is not None:
                action = "reuse"
            elif document_id in baseline:
                action = "adopt_existing"
            elif document_id not in old_documents:
                action = "new"
            else:
                action = "changed_or_cache_invalid"
            plans.append(
                {
                    "document_id": document_id,
                    "action": action,
                    "fingerprint": self.fingerprint(document),
                }
            )
        return plans

    @contextmanager
    def lock(self):
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / "update.lock"
        stream = path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("已有进程正在更新知识库，请等待其完成") from exc
        try:
            yield
        finally:
            # 操作系统锁会在进程退出时释放，崩溃也不会留下无法获取的锁。
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()

    def process(self, document: dict):
        pages = list(extract_document(self.root, document))
        unresolved = [p.page_number for p in pages if p.structure_review_required]
        if unresolved:
            raise ValueError(f"PDF警告页尚未通过结构复核: {document['document_id']} {unresolved}")
        parents = []
        section = ""
        for page in pages:
            chunks, section = split_page(page, ChunkConfig(), section)
            parents.extend(chunks)
        lookup = {(page.document_id, page.page_number): page for page in pages}
        confirmed = load_confirmed_headers(self.root, document, lookup)
        children = []
        for parent in parents:
            children.extend(
                prepare_chunk(
                    parent,
                    lookup,
                    self.model.count_tokens,
                    self.model_manifest["model_id"],
                    self.model_manifest["revision"],
                    min(self.token_budget, self.model.token_budget),
                    confirmed_headers=confirmed,
                )
            )
        if not children:
            raise ValueError(f"文档未产生有效片段: {document['document_id']}")
        # 编码前逐父片段查覆盖，不能让检索评测恰好未问到的漏字被掩盖。
        for parent in parents:
            coverage = bytearray(len(parent.text))
            for child in children:
                if child.parent_chunk_id == parent.chunk_id:
                    offset = child.start_char - parent.start_char
                    coverage[offset : offset + len(child.text)] = b"\x01" * len(child.text)
            if any(not c.isspace() and not coverage[i] for i, c in enumerate(parent.text)):
                raise ValueError(f"父片段非空白内容丢失: {parent.chunk_id}")
        vectors = self.model.encode([child.embedding_text for child in children])
        validate_vectors(vectors, len(children), self.model.dimension)
        return pages, parents, children, vectors

    def _publish(
        self, staged: Path, extra_exports: tuple[str, ...] = (), validate: Callable | None = None
    ) -> None:
        if any(relative != RETRIEVAL_PROFILE for relative in extra_exports):
            raise ValueError("未知发布产物")
        if (self.root / RETRIEVAL_PROFILE).exists() and RETRIEVAL_PROFILE not in extra_exports:
            raise ValueError("已启用受控检索发布，必须同时发布已验收检索配置")
        exports = (*EXPORTS, *extra_exports)
        journal_path = self.base / "pending_publish.json"
        backup = staged / "backups"
        existing = {}
        for relative in exports:
            current = self.root / relative
            existing[relative] = current.exists()
            if current.exists():
                target = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, target)
        _write_json(journal_path, {"run_dir": str(staged), "existing": existing})
        try:
            for relative in exports:
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".publish.tmp")
                shutil.copy2(staged / relative, temporary)
                temporary.replace(target)
            if validate is not None:
                validate()
            journal_path.unlink()
        except BaseException:
            self.recover()
            raise

    def recover(self) -> None:
        journal_path = self.base / "pending_publish.json"
        if not journal_path.exists():
            return
        journal = _json(journal_path)
        staged = Path(journal["run_dir"]).resolve()
        if not staged.is_relative_to((self.base / "runs").resolve()):
            raise ValueError("发布恢复路径超出本项目运行目录")
        for relative, existed in journal["existing"].items():
            if relative not in (*EXPORTS, RETRIEVAL_PROFILE):
                raise ValueError("发布恢复文件不在允许列表")
            target = self.root / relative
            if existed:
                shutil.copy2(staged / "backups" / relative, target)
            else:
                target.unlink(missing_ok=True)
        journal_path.unlink()

    def active_matches(self, results: list) -> bool:
        """失败后产生的缓存不等于已发布版本，必须核对活动产物。"""
        try:
            index = _json(self.root / "data/chroma/index_manifest.json")
            manifest = _json(self.root / "data/embeddings/manifest.json")
            if manifest["weights_sha256"] != self.weights_hash:
                return False
            if index["source_sha256"] != file_sha256(self.root / EXPORTS[2]) or index[
                "vectors_sha256"
            ] != file_sha256(self.root / EXPORTS[3]):
                return False
            for offset, record_type in enumerate((PageRecord, ChunkRecord, EmbeddingChunk)):
                expected = [record.model_dump() for result in results for record in result[offset]]
                if [
                    record.model_dump()
                    for record in _records(self.root / EXPORTS[offset], record_type)
                ] != expected:
                    return False
            with np.load(self.root / EXPORTS[3], allow_pickle=False) as archive:
                if archive["chunk_ids"].tolist() != [
                    child.chunk_id for result in results for child in result[2]
                ]:
                    return False
                expected_vectors = np.concatenate([result[3] for result in results])
                return index["count"] == len(expected_vectors) and np.array_equal(
                    archive["vectors"], expected_vectors
                )
        except (OSError, ValueError, KeyError):
            return False

    def update(self, dry_run: bool = False) -> dict:
        if not dry_run and (self.root / RETRIEVAL_PROFILE).exists():
            raise ValueError("默认索引受验收门槛保护；请构建候选并使用安全激活入口")
        if dry_run:
            if (self.base / "pending_publish.json").exists():
                raise ValueError("存在中断的发布，先运行正常更新进行恢复")
            documents = self.documents()
            baseline = self.baseline(documents)
            plans = self.plan(documents, baseline)
            return {
                "dry_run": True,
                "plans": plans,
                "counts": dict(Counter(plan["action"] for plan in plans)),
            }
        with self.lock():
            self.recover()
            documents = self.documents()
            baseline = self.baseline(documents)
            plans = self.plan(documents, baseline)
            results, processed_ids, encoded = [], [], 0
            for document, plan in zip(documents, plans, strict=True):
                result = self.read_cache(document)
                if result is None:
                    if plan["action"] == "adopt_existing":
                        result = baseline[document["document_id"]]
                    else:
                        result = self.process(document)
                        encoded += len(result[2])
                        processed_ids.append(document["document_id"])
                    self.write_cache(document, result)
                results.append(result)
            state = {
                "profile": self.profile,
                "documents": {
                    document["document_id"]: {
                        "fingerprint": self.fingerprint(document),
                        "sha256": document["sha256"],
                    }
                    for document in documents
                },
            }
            report = {
                "dry_run": False,
                "plans": plans,
                "counts": dict(Counter(plan["action"] for plan in plans)),
                "processed_documents": processed_ids,
                "encoded_vectors": encoded,
                "total_documents": len(documents),
                "total_vectors": sum(len(item[2]) for item in results),
            }
            # 首次迁移或全部未变化：只记录缓存状态，不改写导出或Chroma。
            if not processed_ids and self.active_matches(results):
                _write_json(self.base / "state.json", state)
                report["index_published"] = False
                _write_json(self.root / "eval/results/incremental_update_report.json", report)
                return report
            staged = self.base / "runs" / uuid4().hex
            staged.mkdir(parents=True)
            pages = [page for result in results for page in result[0]]
            parents = [parent for result in results for parent in result[1]]
            children = [child for result in results for child in result[2]]
            vectors = np.concatenate([result[3] for result in results])
            _write_records(staged / EXPORTS[0], pages)
            _write_records(staged / EXPORTS[1], parents)
            _write_records(staged / EXPORTS[2], children)
            checks = validate_chunks(
                staged / EXPORTS[0], staged / EXPORTS[2], self.root / "eval/dataset.jsonl"
            )
            if checks["integrity_errors"] or checks["answerable_fail"]:
                raise ValueError("增量结果未通过证据检查，旧索引保持可用")
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
                "signature": _fingerprint({"source_hash": source_hash, "profile": self.profile}),
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
            self._publish(staged)
            report.update(
                index_published=True,
                collection_name=ingestion["collection_name"],
                index_strategy="versioned_snapshot_full_sync",
                rollback_backup=str(staged / "backups"),
            )
            _write_json(self.root / "eval/results/incremental_update_report.json", report)
            return report
