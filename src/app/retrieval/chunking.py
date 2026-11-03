"""可追溯的按页切片基线；表格识别是文本启发式，不是版面识别。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel

from app.retrieval.pdf_extract import PageRecord

CHUNKING_VERSION = "page-heading-table-v2"
HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百0-9]+[节章]\s*|"
    r"[一二三四五六七八九十]+[、．.]|"
    r"[（(][一二三四五六七八九十0-9]+[）)]|\d{1,2}[、．.])"
)
NUMBER = re.compile(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?%?")


@dataclass(frozen=True)
class ChunkConfig:
    chunk_size: int = 800
    chunk_overlap: int = 100

    def __post_init__(self) -> None:
        if self.chunk_size <= 0 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("要求 chunk_size > 0 且 0 <= chunk_overlap < chunk_size")


class ChunkRecord(BaseModel):
    chunk_id: str
    document_id: str
    stock_code: str
    stock_name: str
    document_type: str
    title: str
    report_period: str
    publish_date: str
    source_url: str
    local_path: str
    section: str
    section_paths: list[str]
    page_start: int
    page_end: int
    start_char: int
    end_char: int
    text: str
    character_count: int
    content_hash: str
    chunk_type: Literal["text", "table_candidate"]
    oversized: bool
    chunking_version: str
    chunk_size: int
    chunk_overlap: int


def _is_heading(line: str) -> bool:
    # 排除目录项目、表格数字和长段落，避免财务数据被当成标题。
    return (
        len(line) <= 70
        and bool(HEADING.match(line))
        and not re.search(r"\.{3,}|…|[-+]?\d[\d,]*\.\d{2}", line)
    )


def _sections(text: str, inherited: str) -> list[tuple[int, int, str]]:
    sections = []
    start = offset = 0
    section = inherited
    for line in text.splitlines(keepends=True):
        if _is_heading(line.strip()):
            if text[start:offset].strip():
                sections.append((start, offset, section))
            start, section = offset, line.strip()
        offset += len(line)
    if text[start:].strip():
        sections.append((start, len(text), section))
    return sections


def _table_candidate(block: str) -> bool:
    # 没有坐标信息时，只保守识别单位、表头和密集数值段落。
    if "单位：" in block or "单位:" in block:
        return True
    if "。" in block or "；" in block:
        return False
    # 业务许可发展时间线包含多个年份，但缺少金额列，按叙述安全切分。
    if (
        len(re.findall(r"(?:19|20)\d{2}\s*年", block)) >= 3
        and re.search(r"许可|获|资质", block)
        and not re.search(r"\d[\d,]*\.\d+|\d,\d|\d\s*%", block)
    ):
        return False
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if any(len(NUMBER.findall(line)) >= 2 for line in lines):
        return True
    if any(re.search(r"\d[\d,]*\.\d+|\d+%", line) for line in lines):
        return True
    return any(word in block for word in ("本期营业收入", "主要会计数据", "毛利率比上年"))


def _regions(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """合并连续的疑似表格段落，保留原文偏移。"""
    blocks = list(re.finditer(r"\S(?:.*?)(?=\n\s*\n|\Z)", text[start:end], re.S))
    regions: list[tuple[int, int, str]] = []
    for block in blocks:
        block_start, block_end = start + block.start(), start + block.end()
        kind = "table_candidate" if _table_candidate(block.group()) else "text"
        if regions and regions[-1][2] == kind:
            previous = regions.pop()
            regions.append((previous[0], block_end, kind))
        else:
            regions.append((block_start, block_end, kind))
    return regions


def split_page(
    page: PageRecord, config: ChunkConfig, inherited_section: str = ""
) -> tuple[list[ChunkRecord], str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        keep_separator="end",
        add_start_index=True,
        strip_whitespace=False,
    )
    records = []
    sections = _sections(page.text, inherited_section)
    for section_start, section_end, section in sections:
        for start, end, kind in _regions(page.text, section_start, section_end):
            region = page.text[start:end]
            spans = [(0, region)]
            if kind == "text":
                spans = [
                    (part.metadata["start_index"], part.page_content)
                    for part in splitter.create_documents([region])
                ]
                # 最后一小片（例如页脚“10”）可能在重叠区找到同样数字。
                # 末片以原文后缀定位，避免错误合并把前片末尾截短。
                if spans:
                    _, final = spans[-1]
                    if region.rstrip().endswith(final.rstrip()):
                        spans[-1] = (len(region.rstrip()) - len(final.rstrip()), final)
            for relative_start, content in spans:
                if relative_start < 0:
                    raise ValueError(f"无法定位切片原文: {page.page_id}")
                left_trim = len(content) - len(content.lstrip())
                content = content.strip()
                if not content:
                    continue
                chunk_start = start + relative_start + left_trim
                chunk_end = chunk_start + len(content)
                if page.text[chunk_start:chunk_end] != content:
                    raise ValueError(f"切片与原文不一致: {page.page_id}")
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                identity = (
                    f"{page.page_id}:{chunk_start}:{chunk_end}:{digest}:"
                    f"{CHUNKING_VERSION}:{config.chunk_size}:{config.chunk_overlap}"
                )
                records.append(
                    ChunkRecord(
                        chunk_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                        **page.model_dump(
                            exclude={
                                "page_id",
                                "page_number",
                                "text",
                                "character_count",
                                "content_hash",
                            }
                        ),
                        section=section,
                        section_paths=[section] if section else [],
                        page_start=page.page_number,
                        page_end=page.page_number,
                        start_char=chunk_start,
                        end_char=chunk_end,
                        text=content,
                        character_count=len(content),
                        content_hash=digest,
                        chunk_type=kind,
                        oversized=len(content) > config.chunk_size,
                        chunking_version=CHUNKING_VERSION,
                        chunk_size=config.chunk_size,
                        chunk_overlap=config.chunk_overlap,
                    )
                )
    # 文本提取常把表头、单位和一行数据拆成多个段落；合并同页的小片段。
    # 直接取原文区间，保留段落间隔和完整表格，同时减少碎片化。
    merged: list[ChunkRecord] = []
    for record in records:
        if (
            merged
            and record.chunk_type == merged[-1].chunk_type
            and not re.match(r"^第[一二三四五六七八九十百0-9]+[节章]", record.text)
            and record.end_char >= merged[-1].end_char
            and record.end_char - merged[-1].start_char <= config.chunk_size
        ):
            previous = merged.pop()
            content = page.text[previous.start_char : record.end_char]
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            identity = (
                f"{page.page_id}:{previous.start_char}:{record.end_char}:{digest}:"
                f"{CHUNKING_VERSION}:{config.chunk_size}:{config.chunk_overlap}"
            )
            section_paths = list(dict.fromkeys(previous.section_paths + record.section_paths))
            merged.append(
                previous.model_copy(
                    update={
                        "chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                        "end_char": record.end_char,
                        "section": " | ".join(section_paths),
                        "section_paths": section_paths,
                        "text": content,
                        "character_count": len(content),
                        "content_hash": digest,
                        "chunk_type": "table_candidate"
                        if "table_candidate" in (previous.chunk_type, record.chunk_type)
                        else "text",
                    }
                )
            )
        else:
            merged.append(record)
    return merged, sections[-1][2] if sections else inherited_section


def chunk_pages(input_path: Path, output_path: Path, config: ChunkConfig) -> dict:
    """不跨页切分，继承当前文档章节；失败时不替换已有输出。"""
    if input_path.resolve() == output_path.resolve():
        raise ValueError("输入文件和输出文件不能相同")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    counts: Counter = Counter()
    sections: dict[str, str] = {}
    page_ids: set[str] = set()
    chunk_ids: set[str] = set()
    try:
        with (
            input_path.open(encoding="utf-8") as source,
            temporary.open("w", encoding="utf-8", newline="\n") as target,
        ):
            for line in source:
                if not line.strip():
                    continue
                page = PageRecord.model_validate_json(line)
                if page.page_id in page_ids:
                    raise ValueError(f"重复页面ID: {page.page_id}")
                page_ids.add(page.page_id)
                chunks, section = split_page(page, config, sections.get(page.document_id, ""))
                sections[page.document_id] = section
                counts["page_count"] += 1
                counts["empty_pages"] += not bool(page.text.strip())
                for chunk in chunks:
                    if chunk.chunk_id in chunk_ids:
                        raise ValueError(f"重复切片ID: {chunk.chunk_id}")
                    chunk_ids.add(chunk.chunk_id)
                    target.write(chunk.model_dump_json() + "\n")
                    counts["chunk_count"] += 1
                    counts["table_candidates"] += chunk.chunk_type == "table_candidate"
                    counts["oversized_chunks"] += chunk.oversized
                    counts["short_chunks_under_100"] += chunk.character_count < 100
                    counts["max_chunk_characters"] = max(
                        counts["max_chunk_characters"], chunk.character_count
                    )
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"config": config.__dict__, "chunking_version": CHUNKING_VERSION, **counts}


def _normalize(text: str) -> str:
    # 原文的百分号可能在表头，日期数字间可能有排版空格。
    normalized = re.sub(r"\s+|[,，%％]", "", text)
    return re.sub(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})",
        lambda match: f"{match[1]}年{int(match[2])}月{int(match[3])}日",
        normalized,
    )


def validate_chunks(pages_path: Path, chunks_path: Path, dataset_path: Path) -> dict:
    """校验源文覆盖与评测证据保留；不是检索召回率或问答准确率。"""
    pages = [
        PageRecord.model_validate_json(line)
        for line in pages_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chunks = [
        ChunkRecord.model_validate_json(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    page_lookup = {(p.document_id, p.page_number): p for p in pages}
    grouped: dict[tuple[str, int], list[ChunkRecord]] = {}
    errors = []
    if len(page_lookup) != len(pages):
        errors.append("重复页面")
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        errors.append("重复切片ID")
    for chunk in chunks:
        key = (chunk.document_id, chunk.page_start)
        page = page_lookup.get(key)
        if page is None:
            errors.append(f"切片无来源页: {chunk.chunk_id}")
            continue
        grouped.setdefault(key, []).append(chunk)
        if (
            chunk.page_start != chunk.page_end
            or not 0 <= chunk.start_char < chunk.end_char <= len(page.text)
            or page.text[chunk.start_char : chunk.end_char] != chunk.text
            or chunk.stock_code != page.stock_code
            or chunk.source_url != page.source_url
            or chunk.character_count != len(chunk.text)
            or hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != chunk.content_hash
            or (chunk.oversized and chunk.chunk_type != "table_candidate")
        ):
            errors.append(f"切片内容或元数据异常: {chunk.chunk_id}")
    for key, page in page_lookup.items():
        covered = bytearray(len(page.text))
        for chunk in grouped.get(key, []):
            covered[chunk.start_char : chunk.end_char] = b"\x01" * len(chunk.text)
        if any(not char.isspace() and not covered[i] for i, char in enumerate(page.text)):
            errors.append(f"页面非空白字符丢失: {page.page_id}")

    results = []
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if not case["answerable"]:
            results.append({"id": case["id"], "status": "not_applicable_refusal"})
            continue
        keys = [(item["document_id"], item["page"]) for item in case["expected_sources"]]
        source_text = _normalize("\n".join(page_lookup[key].text for key in keys))
        texts = [_normalize(c.text) for key in keys for c in grouped.get(key, [])]
        lost, absent = [], []
        for keyword in case["expected_keywords"]:
            normalized = _normalize(keyword)
            if normalized not in source_text:
                absent.append(keyword)
            elif not any(normalized in text for text in texts):
                lost.append(keyword)
        missing_pages = [f"{doc}:p{num}" for doc, num in keys if not grouped.get((doc, num))]
        results.append(
            {
                "id": case["id"],
                "question": case["question"],
                "status": "fail" if lost or missing_pages else "pass",
                "keywords_lost_by_chunking": lost,
                "keywords_not_literal_in_source": absent,
                "source_pages_without_chunks": missing_pages,
            }
        )
    return {
        "scope": "切片完整性和指定来源页证据保留；尚未测试检索、推理或拒答",
        "integrity_errors": errors,
        "answerable_pass": sum(r["status"] == "pass" for r in results),
        "answerable_fail": sum(r["status"] == "fail" for r in results),
        "refusal_not_evaluated": sum(r["status"] == "not_applicable_refusal" for r in results),
        "cases_with_nonliteral_keywords": sum(
            bool(r.get("keywords_not_literal_in_source")) for r in results
        ),
        "cases": results,
    }
