"""从问题和原文标签识别直接字段证据，不读取评测集或标准值。"""

from __future__ import annotations

import re
import unicodedata


def field_evidence_kind(query: str) -> str | None:
    if re.search(r"原因|为何|为什么|风险|变化|增长|占比|比例|季度|母公司|退休|境内|境外", query):
        return None
    if re.search(r"在职员工|员工数量|员工人数", query):
        return "employees"
    if "股权登记日" in query:
        return "record_date"
    if re.search(r"现金红利发放日|红利发放日", query):
        return "payment_date"
    if re.search(r"总资产|资产总额", query):
        return "assets"
    return None


def is_direct_field_evidence(text: str, section: str, kind: str, document_type: str) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if kind in {"employees", "assets"} and document_type != "annual_report":
        return False
    if kind == "employees":
        return bool(re.search(
            r"在职员工(?:的)?数量合计(?:\(人\))?[:：]?\d"
            r"|共有在职员工\d"
            r"|员工人数\(人\).{0,100}\d", compact
        ))
    if kind == "assets":
        if re.search(r"母公司|子公司|财务公司|联营|合营", section):
            return False
        return bool(re.search(
            r"(?:^|\n)\s*(?:总资产|资产总额)\s+\d"
            r"|总资产规模(?:达到|达到约|约为|达|为|突破)?\d", text
        ))
    if kind in {"record_date", "payment_date"}:
        if document_type != "announcement":
            return False
        label = "股权登记日" if kind == "record_date" else "现金红利发放日"
        date = r"20\d{2}(?:年\d{1,2}月\d{1,2}日|[/.-]\d{1,2}[/.-]\d{1,2})"
        # 正文日期须直接关联字段；表格须有字段表头及紧随的A股日期行。
        return bool(re.search(
            re.escape(label) + r"(?:即|为|是|[:：]|\(即\))?" + date, compact
        ) or re.search(
            re.escape(label) + r"[^\d]{0,70}A股" + date, compact
        ))
    return False
