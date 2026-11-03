"""Freeze portable local state; exclude downloaded PDFs, weights, secrets and environments."""

import gzip
import hashlib
import json
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIRS = (
    "data/chroma",
    "data/embeddings",
    "data/incremental",
    "data/processed",
    "data/table_audit",
    "eval/results",
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    output = ROOT / ".cloud/state"
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob("snapshot.part*")):
        raise RuntimeError("Snapshot already exists; preserve it before creating another version")
    paths = sorted(p for folder in STATE_DIRS for p in (ROOT / folder).rglob("*") if p.is_file())
    manifest = {"version": 1, "files": [], "parts": []}
    with tempfile.TemporaryFile() as buffer:
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for path in paths:
                    relative = path.relative_to(ROOT).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.mtime = info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                    manifest["files"].append(
                        {"path": relative, "size": path.stat().st_size, "sha256": digest(path)}
                    )
        buffer.seek(0)
        index = 0
        while block := buffer.read(1024 * 1024):
            path = output / f"snapshot.part{index:04d}"
            path.write_bytes(block)
            manifest["parts"].append(
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "size": len(block),
                    "sha256": digest(path),
                }
            )
            index += 1
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "files": len(paths),
                "parts": index,
                "compressed_bytes": sum(p["size"] for p in manifest["parts"]),
            }
        )
    )


if __name__ == "__main__":
    main()
