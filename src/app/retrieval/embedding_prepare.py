"""按真实token长度生成子片段，保留父片段和来源偏移。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from app.retrieval.chunking import ChunkRecord, _is_heading, validate_chunks
from app.retrieval.pdf_extract import PageRecord

PREPARATION_VERSION = "token-budget-logical-tables-v2"
AMOUNT = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)%?")
NUMERIC_TOKEN = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")


class HeaderEvidence(BaseModel):
    document_id: str
    page_number: int
    start_char: int
    end_char: int
    text: str


class EmbeddingChunk(ChunkRecord):
    parent_chunk_id: str
    parent_was_oversized: bool
    preparation_version: str
    embedding_text: str
    embedding_token_count: int
    token_budget: int
    embedding_model: str
    embedding_revision: str
    repeated_header: HeaderEvidence | None
    table_layout_review_required: bool
    table_context_status: str = "legacy_unreviewed"


def _table_title(line: str) -> bool:
    return (
        len(line) <= 65
        and not re.search(r"\d[\d,]*\.\d{2}", line)
        and bool(
            re.search(
                r"情况表$|(?:学历|年龄)结构$|(?:合并|母公司)?(?:资产负债表|利润表|现金流量表)$",
                line,
            )
        )
    )


def _logical_boundaries(page: PageRecord) -> list[tuple[int, str]]:
    boundaries = [(0, "page")]
    offset = 0
    for line in page.text.splitlines(keepends=True):
        stripped = line.strip()
        start = offset + len(line) - len(line.lstrip())
        if re.match(r"单位\s*[:：]", stripped):
            boundaries.append((start, "unit"))
        elif _table_title(stripped):
            boundaries.append((start, "table_title"))
        elif _is_heading(stripped):
            boundaries.append((start, "section"))
        offset += len(line)
    return sorted(dict(boundaries).items())


def _data_line(line: str) -> bool:
    # 年度列名含数字仍属于表头；小数、百分数、金额及人员数量才触发正文。
    if re.search(r"\d[\d,]*\.\d+|\d\s*%|\d,\d", line):
        return True
    # 资产负债表列名里的完整日期也属于表头。
    line = re.sub(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", "", line)
    values = list(re.finditer(r"\d+", line))
    if not values:
        return False
    return not all(
        1990 <= int(match.group()) <= 2099 and re.match(r"\s*年", line[match.end() :])
        for match in values
    )


def _header_on_page(page: PageRecord, before: int) -> HeaderEvidence | None:
    candidates = [(start, kind) for start, kind in _logical_boundaries(page) if start <= before]
    start, kind = candidates[-1]
    if kind not in ("unit", "table_title"):
        return None
    # 单位与标题只在当前逻辑表内关联；下一张表/章节立即重置。
    boundary_end = next(
        (position for position, _ in _logical_boundaries(page) if position > start), len(page.text)
    )
    end = start
    for number, line in enumerate(page.text[start:boundary_end].splitlines(keepends=True)):
        if re.match(r"\s*(?:非)?流动(?:资产|负债)[:：]|\s*(?:所有者权益|股东权益).*[:：]", line):
            break
        if number and _data_line(line):
            break
        if end - start + len(line) > 250:
            break
        end += len(line)
    text = page.text[start:end].strip()
    if not text:
        return None
    return HeaderEvidence(
        document_id=page.document_id,
        page_number=page.page_number,
        start_char=start,
        end_char=start + len(text),
        text=text,
    )


def find_header(
    chunk: ChunkRecord,
    pages: dict[tuple[str, int], PageRecord],
    confirmed_headers: dict[tuple[str, int], HeaderEvidence] | None = None,
) -> HeaderEvidence | None:
    page = pages[(chunk.document_id, chunk.page_start)]
    # 跨页需版面/列名/表名证据，单凭页首位置不足以确认续表。
    local = _header_on_page(page, chunk.start_char)
    if local:
        return local
    return (confirmed_headers or {}).get((chunk.document_id, chunk.page_start))


def load_confirmed_headers(root: Path, document: dict, pages):
    path = root / "data/manifests/table_header_reviews.json"
    if not path.exists():
        return {}
    confirmed = {}
    for review in json.loads(path.read_text(encoding="utf-8"))["reviews"]:
        if (
            review["document_id"] != document["document_id"]
            or review["source_sha256"] != document["sha256"]
        ):
            continue
        source = pages[(document["document_id"], review["source_page"])]
        position = source.text.find(review["unit_anchor"])
        if position < 0:
            raise ValueError("已复核续表的单位锚点不匹配")
        header = _header_on_page(source, position)
        if not header or not all(
            re.sub(r"\s+", "", anchor) in re.sub(r"\s+", "", header.text)
            for anchor in review["column_anchors"]
        ):
            raise ValueError("已复核续表的列名锚点不匹配")
        for number in review["target_pages"]:
            confirmed[(document["document_id"], number)] = header
    return confirmed


def _units(text: str, table: bool) -> list[tuple[int, int]]:
    if not table:
        return [(m.start(), m.end()) for m in re.finditer(r"[^\n]+\n*|\n+", text)]
    # 优先把换行的行标签与数值放在一起；所有提取行都完整保留。
    # 多列PDF的真实单元格关系仍需版面解析，这里不声称已经恢复。
    units = []
    start = offset = 0
    for line in text.splitlines(keepends=True):
        offset += len(line)
        if AMOUNT.search(line) or not line.strip():
            if text[start:offset].strip():
                units.append((start, offset))
            start = offset
    if start < len(text):
        units.append((start, len(text)))
    return units


def _safe_prose_spans(
    text: str, start: int, end: int, fits: Callable[[str], bool]
) -> list[tuple[int, int]]:
    spans = []
    while start < end:
        low, high, cut = start + 1, end, start
        while low <= high:
            middle = (low + high) // 2
            if fits(text[start:middle]):
                cut, low = middle, middle + 1
            else:
                high = middle - 1
        for number in NUMERIC_TOKEN.finditer(text, start, end):
            if number.start() < cut < number.end():
                cut = number.start()
                break
        if cut <= start:
            raise ValueError("单个数字或字符超过token预算，需要人工检查")
        spans.append((start, cut))
        start = cut
    return spans


def _prepare_region(
    chunk: ChunkRecord,
    pages: dict[tuple[str, int], PageRecord],
    count_tokens: Callable[[str], int],
    model_id: str,
    revision: str,
    budget: int = 480,
    confirmed_headers=None,
) -> list[EmbeddingChunk]:
    context = (
        f"公司：{chunk.stock_name}（{chunk.stock_code}）；报告期：{chunk.report_period}；"
        f"文档：{chunk.title}；页码：{chunk.page_start}\n"
    )
    table = chunk.chunk_type == "table_candidate"
    header = find_header(chunk, pages, confirmed_headers) if table else None
    prefix = context + ("原文表头：\n" + header.text + "\n正文：\n" if header else "正文：\n")
    # 表头若太大，明确阻止本次入库，不能静默截掉表头或改写它。
    if count_tokens(prefix) >= budget - 64:
        raise ValueError(f"表头或上下文过长，需复核: {chunk.chunk_id}")

    def fits(body: str) -> bool:
        return len(body.strip()) <= 800 and count_tokens(prefix + body.strip()) <= budget

    if fits(chunk.text) and not chunk.oversized:
        spans = [(0, len(chunk.text))]
    else:
        units = _units(chunk.text, table)
        atomic = []
        for start, end in units:
            if fits(chunk.text[start:end]):
                atomic.append((start, end))
            elif table:
                # 段落中的空行分组可能很长，进一步降为完整物理行。
                offset = start
                for line in chunk.text[start:end].splitlines(keepends=True):
                    if not fits(line):
                        raise ValueError(f"表格单行超限，需版面解析: {chunk.chunk_id}")
                    atomic.append((offset, offset + len(line)))
                    offset += len(line)
            else:
                atomic.extend(_safe_prose_spans(chunk.text, start, end, fits))
        spans = []
        for start, end in atomic:
            if spans and fits(chunk.text[spans[-1][0] : end]):
                previous = spans.pop()
                spans.append((previous[0], end))
            else:
                spans.append((start, end))

    prepared = []
    for start, end in spans:
        raw = chunk.text[start:end]
        start += len(raw) - len(raw.lstrip())
        body = raw.strip()
        if not body:
            continue
        end = start + len(body)
        payload = prefix + body
        token_count = count_tokens(payload)
        if token_count > budget:
            raise ValueError(f"子片段仍超限: {chunk.chunk_id}")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        identity = (
            f"{chunk.chunk_id}:{chunk.start_char + start}:{chunk.start_char + end}:"
            f"{model_id}:{revision}:"
            f"{budget}:{PREPARATION_VERSION}:{payload}"
        )
        fields = chunk.model_dump()
        fields.update(
            chunk_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            start_char=chunk.start_char + start,
            end_char=chunk.start_char + end,
            text=body,
            character_count=len(body),
            content_hash=digest,
            oversized=len(body) > 800,
        )
        prepared.append(
            EmbeddingChunk(
                **fields,
                parent_chunk_id=chunk.chunk_id,
                parent_was_oversized=chunk.oversized,
                preparation_version=PREPARATION_VERSION,
                embedding_text=payload,
                embedding_token_count=token_count,
                token_budget=budget,
                embedding_model=model_id,
                embedding_revision=revision,
                repeated_header=header,
                table_layout_review_required=table,
                table_context_status=(
                    "confirmed_cross_page_header"
                    if header and header.page_number != chunk.page_start
                    else "local_text_boundary_candidate"
                )
                if header
                else ("no_confirmed_local_header" if table else "not_table"),
            )
        )
    return prepared


def prepare_chunk(
    chunk: ChunkRecord,
    pages: dict[tuple[str, int], PageRecord],
    count_tokens: Callable[[str], int],
    model_id: str,
    revision: str,
    budget: int = 480,
    confirmed_headers=None,
) -> list[EmbeddingChunk]:
    if chunk.chunk_type != "table_candidate":
        return _prepare_region(
            chunk, pages, count_tokens, model_id, revision, budget, confirmed_headers
        )
    page = pages[(chunk.document_id, chunk.page_start)]
    cuts = (
        [chunk.start_char]
        + [
            position
            for position, _ in _logical_boundaries(page)
            if chunk.start_char < position < chunk.end_char
        ]
        + [chunk.end_char]
    )
    result = []
    for start, end in zip(cuts, cuts[1:], strict=False):
        raw = page.text[start:end]
        start += len(raw) - len(raw.lstrip())
        body = raw.strip()
        if not body:
            continue
        region = chunk.model_copy(
            update={
                "start_char": start,
                "end_char": start + len(body),
                "text": body,
                "character_count": len(body),
                "content_hash": hashlib.sha256(body.encode()).hexdigest(),
            }
        )
        result.extend(
            _prepare_region(
                region, pages, count_tokens, model_id, revision, budget, confirmed_headers
            )
        )
    return result


def prepare_file(
    input_path: Path,
    pages_path: Path,
    output_path: Path,
    dataset_path: Path,
    count_tokens: Callable[[str], int],
    model_id: str,
    revision: str,
    budget: int,
) -> dict:
    if output_path.resolve() in (input_path.resolve(), pages_path.resolve()):
        raise ValueError("子片段不能覆盖原始输入")
    pages = {
        (page.document_id, page.page_number): page
        for line in pages_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for page in [PageRecord.model_validate_json(line)]
    }
    parents = [
        ChunkRecord.model_validate_json(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    children = []
    stats: Counter = Counter()
    parent_details = []
    for chunk in parents:
        result = prepare_chunk(chunk, pages, count_tokens, model_id, revision, budget)
        children.extend(result)
        stats["input_chunks"] += 1
        stats["input_oversized_tables"] += chunk.oversized
        stats["split_parents"] += len(result) > 1
        if chunk.oversized:
            parent_details.append(
                {
                    "parent_chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "page": chunk.page_start,
                    "input_characters": chunk.character_count,
                    "child_count": len(result),
                    "max_child_tokens": max(c.embedding_token_count for c in result),
                    "header_found": any(c.repeated_header is not None for c in result),
                }
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".jsonl.tmp")
    try:
        temporary.write_text(
            "\n".join(child.model_dump_json() for child in children) + "\n", encoding="utf-8"
        )
        evidence = validate_chunks(pages_path, temporary, dataset_path)
        if evidence["integrity_errors"] or evidence["answerable_fail"]:
            raise ValueError(f"子片段证据校验失败: {json.dumps(evidence, ensure_ascii=False)}")
        # 逐个父片段覆盖检查，防止其他重叠片段掩盖本片段中的内容丢失。
        grouped: dict[str, list[EmbeddingChunk]] = {}
        for child in children:
            grouped.setdefault(child.parent_chunk_id, []).append(child)
        for parent in parents:
            coverage = bytearray(len(parent.text))
            for child in grouped.get(parent.chunk_id, []):
                start = child.start_char - parent.start_char
                coverage[start : start + len(child.text)] = b"\x01" * len(child.text)
                header = child.repeated_header
                if header:
                    page = pages[(header.document_id, header.page_number)]
                    if page.text[header.start_char : header.end_char] != header.text:
                        raise ValueError("重复表头与原文不一致")
            if any(not char.isspace() and not coverage[i] for i, char in enumerate(parent.text)):
                raise ValueError(f"父片段内容丢失: {parent.chunk_id}")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        **stats,
        "output_chunks": len(children),
        "token_budget": budget,
        "max_payload_tokens": max(c.embedding_token_count for c in children),
        "remaining_token_overflow": sum(c.embedding_token_count > budget for c in children),
        "remaining_character_overflow": sum(c.oversized for c in children),
        "original_29_details": parent_details,
        "evidence_validation": evidence,
        "table_layout_note": "已保持提取行并重复可识别原文表头；复杂单元格和跨页关联仍需版面复核",
    }
