import argparse
from pathlib import Path

from app.retrieval.pdf_extract import extract_manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="把清单中的PDF提取为按页JSONL")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/manifests/documents.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/pages.jsonl"),
    )
    args = parser.parse_args()

    stats = extract_manifest(args.manifest, args.output)
    print(
        f"提取完成: {stats['document_count']}份文档，"
        f"{stats['page_count']}页，{stats['character_count']}个字符；"
        f"输出={args.output}"
    )
