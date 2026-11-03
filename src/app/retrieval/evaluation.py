"""检索后的字段诊断；禁止向检索器传入这些标准答案字段。"""

from __future__ import annotations

import re
import unicodedata


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("−", "-").replace("–", "-")
    # 保留单元格间空白，避免两个金额被拼接成一个更长的数字。
    return re.sub(r"\s+", " ", re.sub(r"[,，]", "", text)).lower().strip()


def _contains_value(text: str, value: str) -> bool:
    value = re.sub(r"\s+", "", value)
    pattern = r"\s*".join(re.escape(char) for char in value)
    if value and (value[0].isdigit() or value[0] in "-+"):
        pattern = r"(?<![\d.])" + pattern
    if value and value[-1].isdigit():
        pattern += r"(?![\d.])"
    return bool(value) and re.search(pattern, text) is not None


def evaluate_answer_fields(case: dict, hits, groups: list[dict]) -> list[dict]:
    fields = case.get("expected_answer_fields") or [
        {"field": f"legacy-keyword-{number}", "acceptable_values": [keyword]}
        for number, keyword in enumerate(case.get("expected_keywords", []), start=1)
    ]
    by_id = {group["group_id"]: group["sources"] for group in groups}
    results = []
    for field in fields:
        required_ids = field.get("evidence_group_ids", [])
        if any(group_id not in by_id for group_id in required_ids):
            raise ValueError("答案字段引用了不存在的证据组")
        allowed = (
            set().union(*(by_id[group_id] for group_id in required_ids))
            if required_ids else None
        )
        texts = []
        for hit in hits:
            if allowed is not None and (hit.document_id, hit.page_start) not in allowed:
                continue
            texts.append(hit.text)
            texts.extend(item.text for item in getattr(hit, "same_page_supporting_chunks", []))
        text = _normal("\n".join(texts))
        matched = [
            value for value in field["acceptable_values"]
            if _contains_value(text, _normal(str(value)))
        ]
        results.append({"field": field["field"], "matched": bool(matched),
                        "matched_values": matched})
    return results
