"""渲染评测集的唯一来源页，供人工核对数值、行列、单位和年份。"""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "eval/dataset_v2.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/evidence_v2")
    args = parser.parse_args()
    executable = os.environ.get("PDFTOPPM") or shutil.which("pdftoppm")
    if not executable and os.name == "nt":
        bundled = (
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native"
            / "poppler/Library/bin/pdftoppm.exe"
        )
        if bundled.is_file():
            executable = str(bundled)
    if not executable:
        raise RuntimeError("找不到pdftoppm")
    cases = [
        json.loads(line)
        for line in args.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
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
    sources = sorted(
        {
            (source["document_id"], source["page"])
            for case in cases
            for source in case["expected_sources"]
        }
    )
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    for document_id, page in sources:
        document = documents[document_id]
        prefix = args.output / f"{document_id}-p{page}"
        result = subprocess.run(
            [
                str(executable),
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-r",
                "150",
                "-png",
                str(ROOT / document["local_path"]),
                str(prefix),
            ],
            capture_output=True,
            timeout=60,
        )
        image_path = prefix.with_suffix(".png")
        if result.returncode or not image_path.is_file():
            raise RuntimeError(result.stderr.decode(errors="replace"))
        records.append(
            {
                "document_id": document_id,
                "page": page,
                "source_sha256": document["sha256"],
                "image": image_path.relative_to(ROOT).as_posix(),
                "renderer": "poppler pdftoppm 150dpi",
            }
        )
    (args.output / "render_manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"rendered={len(records)}")


if __name__ == "__main__":
    main()
