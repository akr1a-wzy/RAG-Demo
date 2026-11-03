from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

import pymupdf
from pydantic import BaseModel, Field


class PageRecord(BaseModel):
    """一页PDF文字及其可追溯元数据。"""

    page_id: str
    document_id: str
    stock_code: str
    stock_name: str
    document_type: str
    title: str
    report_period: str
    publish_date: str
    source_url: str
    local_path: str
    page_number: int
    text: str
    character_count: int
    content_hash: str
    extraction_method: str = "pymupdf_sorted_text"
    extraction_warnings: list[str] = Field(default_factory=list)
    structure_review_required: bool = False


def clean_page_text(text: str) -> str:
    """做最小清洗，保留换行结构，避免在提取阶段破坏表格。"""

    normalized = text.replace("\x00", "").replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines()]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def extract_document(project_root: Path, document: dict) -> Iterable[PageRecord]:
    pdf_path = project_root / document["local_path"]
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF不存在: {pdf_path}")

    actual_hash = file_sha256(pdf_path)
    if actual_hash != document["sha256"]:
        raise ValueError(f"PDF哈希不一致: {document['document_id']}")

    reviews_path = project_root / "data/manifests/pdf_page_reviews.json"
    reviews = (
        json.loads(reviews_path.read_text(encoding="utf-8"))["reviews"]
        if reviews_path.exists()
        else []
    )
    alternate_reader = None
    with pymupdf.open(pdf_path) as pdf:
        expected_pages = document.get("page_count")
        if expected_pages is not None and pdf.page_count != expected_pages:
            raise ValueError(
                f"PDF页数不一致: {document['document_id']}，"
                f"清单={expected_pages}，实际={pdf.page_count}"
            )

        for page_index, page in enumerate(pdf, start=1):
            pymupdf.TOOLS.mupdf_warnings(reset=True)
            text = clean_page_text(page.get_text("text", sort=True))
            warning = pymupdf.TOOLS.mupdf_warnings(reset=True)
            method = "pymupdf_sorted_text"
            warnings = [warning] if warning else []
            review_required = bool(warning)
            if warning:
                # 警告页可能实际丢失财务数值，用独立解析器回退并保存方法。
                from pypdf import PdfReader

                if alternate_reader is None:
                    alternate_reader = PdfReader(pdf_path)
                try:
                    alternative = alternate_reader.pages[page_index - 1].extract_text(
                        extraction_mode="layout"
                    )
                    method = "pypdf_layout_fallback"
                except Exception as error:
                    warnings.append(f"pypdf layout失败: {type(error).__name__}: {error}")
                    alternative = alternate_reader.pages[page_index - 1].extract_text()
                    method = "pypdf_plain_fallback"
                if not alternative or not alternative.strip():
                    raise ValueError(f"警告页备用解析为空: {document['document_id']}:p{page_index}")
                text = clean_page_text(alternative)
                review = next(
                    (
                        r
                        for r in reviews
                        if r["document_id"] == document["document_id"]
                        and r["page_number"] == page_index
                        and r["source_sha256"] == actual_hash
                    ),
                    None,
                )
                if review and review["extraction_method"] == method:
                    normalized = re.sub(r"\s+", "", text)
                    if all(
                        re.sub(r"\s+", "", anchor) in normalized
                        for anchor in review["expected_anchors"]
                    ):
                        review_required = False
                    else:
                        warnings.append("人工复核锚点与本次提取不匹配，保持隔离")
            yield PageRecord(
                page_id=f"{document['document_id']}:p{page_index}",
                document_id=document["document_id"],
                stock_code=document["stock_code"],
                stock_name=document["stock_name"],
                document_type=document["document_type"],
                title=document["title"],
                report_period=document["report_period"],
                publish_date=document["publish_date"],
                source_url=document["source_url"],
                local_path=document["local_path"],
                page_number=page_index,
                text=text,
                character_count=len(text),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                extraction_method=method,
                extraction_warnings=warnings,
                structure_review_required=review_required,
            )


def extract_manifest(manifest_path: Path, output_path: Path) -> dict[str, int]:
    """提取清单内全部PDF，原子写入按页JSONL。"""

    manifest_path = manifest_path.resolve()
    project_root = manifest_path.parents[2]
    documents = load_manifest(manifest_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")

    page_count = 0
    character_count = 0
    with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
        for document in documents:
            for page in extract_document(project_root, document):
                stream.write(page.model_dump_json() + "\n")
                page_count += 1
                character_count += page.character_count
    temporary_path.replace(output_path)

    return {
        "document_count": len(documents),
        "page_count": page_count,
        "character_count": character_count,
    }
