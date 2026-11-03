"""问答层的有限消歧；不改已发布检索词表及其冻结版本。"""

import re

from app.retrieval.chroma_store import resolve_query
from app.tools.ticker import CODE_TO_CANONICAL_NAME


def resolve_document_query(question: str, history: list[dict] | None = None) -> dict:
    def qualified(text):
        # 只开放带体系限定的简称；裸“平安”、平安银行仍不在此映射。
        return re.sub(r"(?<!中国)平安体系", "中国平安体系", text)

    original = resolve_query(question, history)
    parsed = resolve_query(qualified(question), [
        {**m, "content": qualified(m.get("content", ""))} for m in history or []
    ])
    # 显式的歧义简称不能悄悄继承上一家公司；允许“它”等真正的代词追问。
    if re.search(r"平安(?!体系)", question) and "中国平安" not in question:
        parsed["stock_codes"] = []
    names = "、".join(CODE_TO_CANONICAL_NAME.get(c, c) for c in parsed["stock_codes"])
    parsed["query"] = f"{names} {parsed['report_period'] or ''}年 {question}" if names else question
    if parsed["stock_codes"] != original["stock_codes"]:
        parsed["resolution_policy"] = "qualified-system-alias-v1"
    return parsed
