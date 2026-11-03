"""仅从问题及已解析实体建立检索任务，不读取评测答案。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.retrieval.field_evidence import field_evidence_kind
from app.retrieval.hybrid import FINANCIAL_QUERY_TERMS
from app.tools.ticker import CODE_TO_CANONICAL_NAME


@dataclass(frozen=True)
class RetrievalTask:
    task_id: str
    query: str
    stock_code: str | None
    report_period: str | None
    kind: str
    metric_terms: tuple[str, ...] = ()
    prefer_primary_financial: bool = False
    field_evidence_kind: str | None = None


def financial_terms(query: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", query)
    return tuple(term for term in FINANCIAL_QUERY_TERMS if term in compact)


def is_financial_fact(query: str) -> bool:
    return bool(financial_terms(query)) and not re.search(
        r"原因|为何|为什么|驱动|风险|分行业|分地区|分产品|分业务|季度|母公司"
        r"|研发|占比|占.*比例|比重|比率|毛利率|净利率|ROE|收益率", query
    )


def plan_query_tasks(
    query: str,
    stock_codes: list[str],
    report_period: str | None,
) -> list[RetrievalTask]:
    metrics = financial_terms(query)
    primary = is_financial_fact(query)
    topics: list[tuple[str, str, tuple[str, ...]]] = []
    if primary:
        growth = " 同比变化" if re.search(r"同比|上年|增减|增长率", query) else ""
        units = " ".join(dict.fromkeys(re.findall(r"(?:万亿|百?万|亿)元", query)))
        source_hint = (
            "资产规模 资产负债 权益" if all(t in {"总资产", "资产总额"} for t in metrics)
            else "主要会计数据和财务指标"
        )
        topics.append(
            ("financial", f"{' '.join(metrics)}{growth} {units} {source_hint}", metrics)
        )
    if re.search(r"在职员工|员工数量|员工人数", query):
        scope = " ".join(re.findall(r"母公司|主要子公司|境内|境外|合计", query))
        topics.append(("employees", f"在职员工数量 员工人数 员工情况 {scope}", ()))
    if re.search(r"分红|利润分配|权益分派|派息|派发|现金股利|现金红利|股权登记日|除权|除息", query):
        date_terms = " ".join(re.findall(
            r"股权登记日|现金红利发放日|红利发放日|除权.{0,3}日|除息日", query
        ))
        qualifiers = " ".join(re.findall(r"A股|每\s*\d+\s*股|每股|含税|税后|全年|末期|中期", query))
        announcement_query = (
            f"权益分派实施 {date_terms} {qualifiers}" if date_terms
            else f"利润分配方案 权益分派实施 {qualifiers}"
        )
        topics.append(("dividend", announcement_query, ()))
    if re.search(r"股东会|股东大会", query):
        topics.append(("shareholders_meeting", "年度股东会 股东大会 召开时间", ()))
    codes = list(dict.fromkeys(stock_codes)) or [None]
    if len(topics) <= 1 and len(codes) == 1:
        # 单主题问题保留原问法，避免丢弃每股、含税、A股等限定。
        kind = topics[0][0] if topics else "general"
        return [
            RetrievalTask(
                "task-1", query, codes[0], report_period, kind, metrics, primary,
                field_evidence_kind(query),
            )
        ]
    if not topics:
        # 未识别主题仍保留原问题；没有可靠规则时不丢弃限定条件。
        return [
            RetrievalTask("task-1", query, None, report_period, "general", metrics, False)
        ]
    tasks = []
    for code in codes:
        name = CODE_TO_CANONICAL_NAME.get(code, code or "")
        for kind, topic, terms in topics:
            task_query = f"{name} {report_period or ''}年 {topic}".strip()
            tasks.append(
                RetrievalTask(
                    f"task-{len(tasks) + 1}", task_query, code, report_period,
                    kind, terms, kind == "financial", field_evidence_kind(task_query),
                )
            )
    return tasks
