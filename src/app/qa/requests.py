"""有限自然财务请求解析；原问题始终保留，未知条件不消失在检索改写中。"""

import re

from app.qa.facts import compact
from app.tools.ticker import ALIAS_TO_CODE, CODE_TO_CANONICAL_NAME

REQUEST_VERSION = "annual-revenue-request-v1"

# 这些词只表达交互、来源和输出方式；业务限制不在这里删除。
SURFACE_TERMS = (
    "按年报原始单位给出数值和引用", "年度报告", "原始单位", "原文出处", "合并口径",
    "集团口径", "营业收入", "该公司", "人民币", "是多少", "是什么", "不换算",
    "请问", "麻烦", "帮我", "我想", "想核对", "查一下", "告诉我", "只看", "仅看",
    "给出", "披露", "根据", "按照", "年报", "原文", "出处", "引用", "数值", "单位",
    "金额", "查询", "核对", "全年", "年度", "合并", "集团", "分别", "对比", "比较",
    "营收", "多少", "千元", "请", "想", "按", "的", "和", "与", "及", "是", "为",
    "它", "那", "呢", "元",
)
SURFACE_PATTERN = re.compile("|".join(re.escape(t) for t in sorted(
    SURFACE_TERMS, key=len, reverse=True)))


def requested_units(query: str) -> set[str]:
    return set(re.findall(r"千元|元", compact(query)))


def financial_request(resolved: dict) -> dict:
    """识别取原值请求。不同年份、额外指标/操作/范围与未知词均保守阻断。"""
    query = resolved["query"]
    text = compact(query)
    codes = list(dict.fromkeys(resolved.get("stock_codes", [])))
    period = resolved.get("report_period")
    request = {
        "version": REQUEST_VERSION, "status": "unsupported", "input_query": query,
        "stock_codes": codes, "report_period": period, "metric": "revenue",
        "operation": "compare_raw_values" if len(codes) > 1 else "raw_value",
        "entity_scope": "consolidated_group",
        "unit": "original", "reason": None, "canonical_query": None,
    }
    if not codes or not period or not re.search(r"营业收入|营收", text):
        request["reason"] = "missing_revenue_or_context"
        return request
    years = set(re.findall(r"(20\d{2})年", text))
    if years and years != {period}:
        request["reason"] = "multiple_or_conflicting_periods"
        return request
    # 否定的转换请求不能被当作转换操作；其他未知限定由残留词检查阻断。
    guarded = text.replace("不换算", "")
    restrictions = (
        ("unsupported_scope",
         r"母公司|子公司|单体|分部|分行业|分地区|分产品|分业务|境内|境外|体系"),
        ("unsupported_period", r"季度|半年|半年度|上半年|下半年|[一二三四1234]季|月|日|期末"),
        ("unsupported_operation",
         r"同比|环比|增长|增减|变化|换算|折算|占比|比例|排名|最高|最低|差额"),
        ("unsupported_basis", r"调整|重述|扣除|剔除|不含|不包括|仅含|仅包括"),
    )
    for reason, pattern in restrictions:
        if re.search(pattern, guarded):
            request["reason"] = reason
            request["unsupported_conditions"] = re.findall(pattern, guarded)
            return request
    for alias in sorted(ALIAS_TO_CODE, key=len, reverse=True):
        text = text.replace(alias, "")
    text = re.sub(r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)", "", text, flags=re.I)
    text = re.sub(r"20\d{2}年(?:度)?", "", text)
    units = requested_units(text)
    if len(units) > 1:
        request["reason"] = "conflicting_units"
        return request
    request["unit"] = next(iter(units), "original")
    residue = SURFACE_PATTERN.sub("", text)
    residue = re.sub(r"[、,。?:;()!\s]", "", residue)
    if residue:
        request.update(reason="unrecognized_conditions", unrecognized_text=residue)
        return request
    names = "与".join(CODE_TO_CANONICAL_NAME.get(c, c) for c in codes)
    request.update(status="supported", reason=None,
                   canonical_query=f"{names}{period}年营业收入是多少？")
    return request
