"""Offline integrity and Python-syntax check for the published source snapshot."""
import ast
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
checked = 0
for name, record in manifest["files"].items():
    path = root / name
    assert path.resolve().is_relative_to(root.resolve()), name
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == record["sha256"], name
    if path.suffix == ".py":
        ast.parse(data, filename=name)
    checked += 1
assert not (root / ".env").exists(), "Real .env must not be published"
print(f"Checked {checked} file hashes and Python syntax; no runtime/model/API calls")
