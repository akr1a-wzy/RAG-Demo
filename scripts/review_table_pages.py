"""用Poppler渲染重点表格页，并生成可追溯的视觉复核图片。"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
POPPLER = os.environ.get("PDFTOPPM") or shutil.which("pdftoppm")
if not POPPLER and os.name == "nt":
    bundled = (
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native"
        / "poppler/Library/bin/pdftoppm.exe"
    )
    if bundled.is_file():
        POPPLER = str(bundled)
OUT = ROOT / "data/table_audit/visual"
TARGETS = [
    ("600519-2025-annual", 12),
    ("600519-2025-annual", 56),
    ("600519-2025-annual", 57),
    ("600519-2025-annual", 58),
    ("000858-2025-annual", 35),
    ("000858-2025-annual", 36),
    ("300750-2025-annual", 24),
    ("300750-2025-annual", 26),
    ("600036-2025-annual", 14),
    ("601318-2025-annual", 17),
    ("000333-2025-annual", 10),
    ("000333-2025-annual", 57),
    ("600050-2025-annual", 77),
    ("600050-2025-annual", 78),
    ("600050-2025-annual", 79),
    ("600050-2025-annual", 139),
    ("600050-2025-annual", 141),
    ("600050-2025-annual", 210),
    ("601318-2025-annual", 15),
    ("600030-2025-annual", 17),
]


def main():
    if not POPPLER:
        raise RuntimeError("Install poppler-utils or set PDFTOPPM to the pdftoppm executable")
    OUT.mkdir(parents=True, exist_ok=True)
    docs = {
        d["document_id"]: d
        for d in (
            json.loads(line)
            for line in (ROOT / "data/manifests/documents.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    records = []
    for doc_id, page in TARGETS:
        prefix = OUT / f"{doc_id}-p{page}"
        result = subprocess.run(
            [
                str(POPPLER),
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-r",
                "110",
                "-png",
                str(ROOT / docs[doc_id]["local_path"]),
                str(prefix),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode or not prefix.with_suffix(".png").exists():
            raise RuntimeError(result.stderr.decode(errors="replace"))
        records.append(
            {
                "document_id": doc_id,
                "page_number": page,
                "source_sha256": docs[doc_id]["sha256"],
                "image": prefix.with_suffix(".png").relative_to(ROOT).as_posix(),
                "renderer": "poppler pdftoppm 110dpi",
                "renderer_messages": result.stderr.decode(errors="replace"),
            }
        )
    for index in range(0, len(records), 2):
        selected = records[index : index + 2]
        canvas = Image.new("RGB", (1860, 1370), "#ddd")
        draw = ImageDraw.Draw(canvas)
        for column, record in enumerate(selected):
            image = Image.open(ROOT / record["image"]).convert("RGB")
            image.thumbnail((920, 1320))
            x = column * 930
            canvas.paste(image, (x, 35))
            draw.text(
                (x + 10, 8), f"{record['document_id']} p{record['page_number']}", fill="black"
            )
        canvas.save(OUT / f"contact-{index // 2 + 1:02d}.png")
    (OUT / "render_manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"rendered {len(records)} source pages / {(len(records) + 1) // 2} contact sheets")


if __name__ == "__main__":
    main()
