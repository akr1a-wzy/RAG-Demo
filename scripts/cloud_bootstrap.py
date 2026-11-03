"""Restore the reviewed snapshot and fetch exact public inputs without rebuilding the index."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or Path(relative).is_absolute():
        raise ValueError(f"Unsafe snapshot path: {relative}")
    return path


def restore():
    manifest = json.loads((ROOT / ".cloud/state/manifest.json").read_text())
    files = {item["path"]: item for item in manifest["files"]}
    for item in files.values():
        path = safe_path(item["path"])
        if path.exists() and digest(path) != item["sha256"]:
            raise RuntimeError(f"Existing state has changed; refusing to overwrite: {path}")
    with tempfile.TemporaryFile() as buffer:
        for part in manifest["parts"]:
            path = safe_path(part["path"])
            if path.stat().st_size != part["size"] or digest(path) != part["sha256"]:
                raise RuntimeError(f"Corrupt snapshot part: {path}")
            with path.open("rb") as source:
                shutil.copyfileobj(source, buffer)
        buffer.seek(0)
        seen = set()
        with tarfile.open(fileobj=buffer, mode="r|gz") as archive:
            for member in archive:
                if not member.isfile() or member.name not in files or member.name in seen:
                    raise ValueError(f"Unexpected archive member: {member.name}")
                seen.add(member.name)
                item = files[member.name]
                target = safe_path(member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(member.name)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    temporary = Path(temp.name)
                    shutil.copyfileobj(source, temp)
                try:
                    if (
                        temporary.stat().st_size != item["size"]
                        or digest(temporary) != item["sha256"]
                    ):
                        raise RuntimeError(f"Snapshot content mismatch: {member.name}")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        if seen != set(files):
            raise RuntimeError("Snapshot is incomplete")
    return len(files)


def fetch_document(document, offline):
    target = safe_path(document["local_path"])
    if target.exists():
        if digest(target) != document["sha256"]:
            raise RuntimeError(f"Existing PDF hash mismatch: {target}")
        return
    if offline:
        raise FileNotFoundError(target)
    url = document["source_url"]
    if not url.startswith("https://static.cninfo.com.cn/"):
        raise ValueError(f"Unexpected PDF source: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".download")
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                temporary.open("wb") as out,
            ):
                shutil.copyfileobj(response, out)
            if digest(temporary) != document["sha256"]:
                raise RuntimeError(f"Downloaded PDF hash mismatch: {target.name}")
            temporary.replace(target)
            time.sleep(0.3)
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def restore_inputs():
    """Recover verified offline inputs when proxy policy blocks their public origins."""
    manifest_path = ROOT / ".cloud/offline_inputs/manifest.json"
    if not manifest_path.exists():
        return 0
    manifest = json.loads(manifest_path.read_text())
    with tempfile.TemporaryFile() as buffer:
        for part in manifest["parts"]:
            path = safe_path(part["path"])
            if path.stat().st_size != part["size"] or digest(path) != part["sha256"]:
                raise RuntimeError(f"Corrupt input part: {path}")
            with path.open("rb") as source:
                shutil.copyfileobj(source, buffer)
        buffer.seek(0)
        if hashlib.file_digest(buffer, "sha256").hexdigest() != manifest["archive_sha256"]:
            raise RuntimeError("Offline input archive hash mismatch")
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            files = json.loads(archive.read(".cloud/offline_inputs_manifest.json"))
            expected = {item["path"] for item in files} | {".cloud/offline_inputs_manifest.json"}
            if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
                raise RuntimeError("Unexpected offline input archive members")
            for item in files:
                relative = item["path"]
                if not relative.startswith(("data/raw/", "data/models/bge-small-zh-v1.5/")):
                    raise ValueError(f"Unexpected offline input path: {relative}")
                target = safe_path(relative)
                if target.exists():
                    if digest(target) != item["sha256"]:
                        raise RuntimeError(f"Existing input has changed: {relative}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    temporary = Path(temp.name)
                    with archive.open(relative) as source:
                        shutil.copyfileobj(source, temp)
                try:
                    if (
                        temporary.stat().st_size != item["size"]
                        or digest(temporary) != item["sha256"]
                    ):
                        raise RuntimeError(f"Offline input hash mismatch: {relative}")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        return len(files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--inputs-only", action="store_true")
    args = parser.parse_args()
    restored = 0 if args.verify_only or args.inputs_only else restore()
    inputs = 0 if args.verify_only else restore_inputs()
    docs = [
        json.loads(line)
        for line in (ROOT / "data/manifests/documents.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    for index, document in enumerate(docs, 1):
        fetch_document(document, args.offline or args.verify_only)
        print(f"PDF {index}/{len(docs)} verified: {document['document_id']}", flush=True)
    model_manifest = json.loads((ROOT / "data/models/embedding_model.json").read_text())
    weights = safe_path(model_manifest["local_path"]) / "model.safetensors"
    if not weights.exists() and not (args.offline or args.verify_only):
        subprocess.run(
            [sys.executable, "scripts/download_embedding_model.py"], cwd=ROOT, check=True
        )
    expected = json.loads((ROOT / "data/embeddings/manifest.json").read_text())["weights_sha256"]
    if digest(weights) != expected:
        raise RuntimeError("Model weights differ from the reviewed baseline")
    report = {
        "verified_offline_input_files": inputs,
        "restored_state_files": restored,
        "verified_pdfs": len(docs),
        "weights_sha256": expected,
        "index_rebuilt": False,
        "note": "Migration preserves the baseline; new reviewed chunks are not yet indexed.",
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
