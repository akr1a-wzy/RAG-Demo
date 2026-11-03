"""下载并固定官方模型快照，后续向量化从项目目录离线加载。"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def main() -> None:
    from huggingface_hub import model_info, snapshot_download

    model_id = "BAAI/bge-small-zh-v1.5"
    target = ROOT / "data/models/bge-small-zh-v1.5"
    manifest_path = ROOT / "data/models/embedding_model.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        revision = manifest["revision"]
    else:
        revision = model_info(model_id).sha
    print(f"下载官方模型: {model_id} revision={revision}", flush=True)
    snapshot_download(
        repo_id=model_id,
        revision=revision,
        local_dir=target,
        allow_patterns=[
            "*.json",
            "*.txt",
            "*.safetensors",
            "1_Pooling/*.json",
            "README.md",
        ],
        max_workers=2,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "model_id": model_id,
                "revision": revision,
                "local_path": "data/models/bge-small-zh-v1.5",
                "downloaded_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"模型已固定到: {target}", flush=True)


if __name__ == "__main__":
    main()
