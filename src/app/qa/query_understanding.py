"""共享查询理解：保留原问、条件与转换记录，不调用模型或数据提供方。"""

import re
import unicodedata
from dataclasses import dataclass

from app.retrieval.chroma_store import resolve_query
from app.tools.ticker import ALIAS_TO_CODE, CODE_TO_CANONICAL_NAME

VERSION = "query-understanding-v1"
DOCUMENT_ALIASES = {
    "revenue": r"营业收入|营收",
    "operating_revenue_total": r"营业总收入",
    "net_profit_parent_excl": r"扣非(?:归母)?净利润|扣除非经常性损益(?:后)?(?:的)?归母净利润"
    r"|归属于(?:上市公司|母公司|本公司|本行)股东的扣除非经常性损益(?:后)?的净利润",
    "net_profit_parent": r"归母(?:净利润|利润)"
    r"|归属于(?:上市公司|母公司|本公司|本行)股东的净利润",
    "operating_cashflow": r"经营(?:活动)?(?:产生的)?(?:现金流量净额|现金流净额|现金流)",
    "total_assets": r"总资产|资产总额|资产总计",
    "net_assets_parent": r"归母净资产"
    r"|归属于(?:上市公司|母公司|本公司|本行)股东(?:的)?(?:净资产|权益)",
    "basic_eps": r"基本每股收益|每股收益|EPS",
    "roe": r"(?:加权平均)?净资产收益率|ROE",
    "dividend_gross": r"每(?:10|十)?股(?:派息|分红|派现)|现金分红|现金股利|分红|派息|利润分配",
    "record_date": r"股权登记日(?:期)?|登记日",
    "ex_dividend_date": r"除权(?:除)?息日(?:期)?|除息日|除权日",
    "payment_date": r"现金红利发放日(?:期)?|红利发放日|发放日|派发日|到账日",
    "meeting_date": r"股东(?:大)?会(?:会议)?(?:召开)?(?:日(?:期)?|时间)|股东会什么时候(?:召开|开)",
}
FORECAST = re.compile(
    r"预测|会不会涨|能涨到|翻倍|买入|卖出|目标价|一定会|必涨|推荐买|未来|明天|明年|下周|下个?月|会涨|会跌"
)
CLOSE = re.compile(r"收(?:盘|市)(?:价|多少钱|多少|是|报|为|$)")
MARKET = re.compile(
    r"现价|股价|行情|价格|报价|最新价|收盘价|收市价|市盈率|市净率|市值|估值"
    r"|(?<![A-Za-z])(?:PE|PB)(?![A-Za-z])|日K|K线|交易日|\d+日(?:收益|涨跌)", re.I,
)
MARKET_ALIASES = {
    "price": r"现价|股价|价格|报价|最新价|收盘|收市",
    "pe_ttm": r"市盈率|(?<![A-Za-z])PE(?![A-Za-z])",
    "pb": r"市净率|(?<![A-Za-z])PB(?![A-Za-z])",
    "market_cap": r"市值",
    "kline": r"日K|K线|交易日|\d+日(?:收益|涨跌)",
}
DOCUMENT = re.compile(
    r"年报|财务|营收|营业收入|营业总收入|员工|人数|人员|利润|资产|负债|现金|分红|股利|派息|公告|薪酬"
)
YEAR = re.compile(r"(?<!\d)(\d{4})\s*年")
EXTREME = re.compile(r"最高|最低|最大|最小|峰值|高点|低点")
TIME_SCOPE = re.compile(
    r"最新|实时|当前|现价|最近|今日|今天|今年|去年|前年|昨日|昨天|前天|上周|上月"
)
UNITS = re.compile(r"亿元|百万元|万元|千元|元")
DURATION = re.compile(r"(?:最近|近|过去|连续)\s*\d+\s*年")
QUALIFIERS = re.compile(
    r"母公司|子公司|单体|季度|半年|Q[1-4]|H[12]|境内|境外|海外|分部|主营|营业总收入"
    r"|构成|占比|比例|排名|平均|税后|扣税|不含|剔除|调整前|重述前|调整后|重述后"
    r"|后复权|不复权|前复权|美元|港元", re.I,
)
PREFIX = re.compile(r"^(?:那|那么|它的|我问的是|问的是|我要的是|要的是|改成|换成|继续)")
FOLLOWUP = re.compile(
    r"(?:那|那么|我问的是|问的是|我要的是|要的是|改成|换成|不对[，,\s]*)?\s*"
    r"(?:(?:\d{4})年(?:的)?|今天|今日|今年|去年|前年)?"
    r"(?:最高|最低|最大|最小|峰值|高点|低点)?(?:价|价格|股价|收盘价)?"
    r"(?:(?:换算|转换|折合|按|用)?(?:亿元|百万元|万元|千元|元)|原值|原文|来源|引用)?"
    r"(?:的|呢|是多少|多少|多少钱|啊|呀|吗|[？?。！!\s])*"
)


def normalize_question(question: str) -> tuple[str, list[dict], list[str]]:
    text = unicodedata.normalize("NFKC", question).strip()
    changes, issues = [], []
    for code in CODE_TO_CANONICAL_NAME:
        text = re.sub(rf"(?<!\d){code}(?=\d{{2,4}}\s*年)", code + " ", text)
    durations = [m.span() for m in DURATION.finditer(text)]

    def year(match):
        raw = match[1]
        if any(a <= match.start() < b for a, b in durations):
            return match[0]
        if len(raw) == 2 and int(raw) >= 40:
            issues.append("ambiguous_century")
            return match[0]
        value = ("20" + raw if len(raw) == 2 else raw) + "年"
        if value != match[0]:
            changes.append({"source": match[0], "target": value, "kind": "year"})
        return value

    text = re.sub(r"(?<!\d)(\d{4}|\d{2})\s*年", year, text)
    for alias in ["营业收入总额", "营收总额", "总营收"]:
        if alias in text:
            changes.append({"source": alias, "target": "营业收入", "kind": "revenue_alias"})
            text = text.replace(alias, "营业收入")
    for code in CODE_TO_CANONICAL_NAME:
        text = re.sub(rf"(?<!\d){code}(?=\d{{4}}年)", code + " ", text)
    return text, changes, issues


def metric_matches(text: str) -> list[tuple[int, int, str]]:
    matches = [(m.start(), m.end(), metric)
               for metric, pattern in DOCUMENT_ALIASES.items()
               for m in re.finditer(pattern + "|" + re.escape(metric), text, re.I)]
    return [m for m in matches if not any(
        a <= m[0] and b >= m[1] and b - a > m[1] - m[0] for a, b, _ in matches
    )]


def domain_of(text: str) -> str:
    market = bool(MARKET.search(text) or CLOSE.search(text))
    document = bool(DOCUMENT.search(text) or metric_matches(text))
    return "mixed" if market and document else "market" if market else (
        "document" if document else "unknown"
    )


def resolve_entities(question: str, history: list[dict] | None = None) -> dict:
    text = normalize_question(question)[0]
    messages = [{**m, "content": normalize_question(m.get("content", ""))[0]}
                for m in history or []]

    def qualified(value):
        return re.sub(r"(?<!中国)平安体系", "中国平安体系", value)

    original = resolve_query(text, messages)
    parsed = resolve_query(qualified(text), [
        {**m, "content": qualified(m.get("content", ""))} for m in messages
    ])
    if re.search(r"平安(?!体系)", text) and "中国平安" not in text:
        parsed["stock_codes"] = []
    names = "、".join(CODE_TO_CANONICAL_NAME.get(c, c) for c in parsed["stock_codes"])
    # 原问仍交给约束筛选和校验；规范化结果由共享理解对象单独携带。
    parsed["query"] = (f"{names} {parsed['report_period'] or ''}年 {question}"
                       if names else question)
    if parsed["stock_codes"] != original["stock_codes"]:
        parsed["resolution_policy"] = "qualified-system-alias-v1"
    return parsed


def effective_question(question: str, history: list[dict]) -> tuple[str, str]:
    context, domain = "", "unknown"
    for message in [m for m in history[-20:] if m.get("role") == "user"] + [
        {"role": "user", "content": question}
    ]:
        text = normalize_question(message.get("content", ""))[0]
        current = domain_of(text)
        correction = bool(FOLLOWUP.fullmatch(text) and text)
        if current == "unknown" and correction and domain in {"market", "document"}:
            inherit = True
        elif current == domain == "market":
            inherit = not TIME_SCOPE.search(text) and (
                EXTREME.search(text)
                or (PREFIX.search(text) and (YEAR.search(context) or EXTREME.search(context)))
            )
        elif current == domain == "document":
            inherit = bool(PREFIX.search(text))
        else:
            inherit = False
        if inherit:
            if YEAR.search(text) or (domain == "market" and TIME_SCOPE.search(text)):
                context = YEAR.sub("", context)
                if domain == "market":
                    context = TIME_SCOPE.sub("", context)
            if EXTREME.search(text):
                context = EXTREME.sub("", context)
            if domain == "document" and metric_matches(text):
                for a, b, _ in sorted(metric_matches(context), reverse=True):
                    context = context[:a] + " " * (b - a) + context[b:]
            if domain == "document" and UNITS.search(text):
                context = UNITS.sub("", context)
            context = f"{context} {text}"
        else:
            context, domain = text, current
    return context, domain


@dataclass(frozen=True)
class QueryUnderstanding:
    original: str
    normalized: str
    effective: str
    domain: str
    resolved: dict
    years: tuple[str, ...]
    metrics: tuple[str, ...]
    operation: str | None
    constraints: tuple[str, ...]
    normalizations: tuple[dict, ...]
    issues: tuple[str, ...]

    def metadata(self) -> dict:
        own = resolve_entities(self.normalized, [])
        inherited = []
        if not own["stock_codes"] and self.resolved["stock_codes"]:
            inherited.append("company")
        if not YEAR.search(self.normalized) and self.years:
            inherited.append("time_scope")
        if not metric_matches(self.normalized) and metric_matches(self.effective):
            inherited.append("metrics")
        if not EXTREME.search(self.normalized) and EXTREME.search(self.effective):
            inherited.append("operation")
        return {"version": VERSION, "original_question": self.original,
                "normalized_question": self.normalized, "effective_question": self.effective,
                "domain": self.domain, "years": list(self.years), "metrics": list(self.metrics),
                "operation": self.operation, "constraints": list(self.constraints),
                "normalizations": list(self.normalizations), "issues": list(self.issues),
                "inherited_conditions": bool(inherited or self.effective != self.normalized),
                "inherited_slots": inherited,
                "time_scope": {
                    "kind": "report_period" if self.domain == "document" else "market_range",
                    "years": list(self.years),
                    "relative": TIME_SCOPE.findall(self.effective),
                    "trading_days": re.findall(r"(\d+)(?:个)?(?:交易)?日", self.effective),
                }}


def understand_query(question: str, history: list[dict] | None = None) -> QueryUnderstanding:
    history = history or []
    normalized, changes, issues = normalize_question(question)
    effective, domain = effective_question(normalized, history)
    resolved = resolve_entities(normalized, history)
    years = list(dict.fromkeys(YEAR.findall(effective)))
    if domain == "document" and not years and resolved["report_period"]:
        years = [resolved["report_period"]]
    if years:
        resolved["report_period"] = years[0]
    operations = EXTREME.findall(effective)
    operation = (operations[-1] if operations else "year_over_year"
                 if re.search(r"同比|增长|增减|变化|变动", effective) else "difference"
                 if re.search(r"差多少|相差|差额|百分点", effective) else "convert_unit"
                 if re.search(r"换算|转换|折合", effective) else None)
    metrics = list(dict.fromkeys(m[2] for m in sorted(metric_matches(effective))))
    if domain in {"market", "mixed"}:
        metrics.extend(k for k, pattern in MARKET_ALIASES.items()
                       if re.search(pattern, effective, re.I))
    return QueryUnderstanding(
        question, normalized, effective, domain, resolved, tuple(years),
        tuple(metrics), operation, tuple(QUALIFIERS.findall(effective)),
        tuple(changes), tuple(issues),
    )


def capability_decision(query: QueryUnderstanding) -> dict | None:
    """共享前置门槛；具体单位、核准字段与窗口仍由执行器严格验证。"""
    def stop(status, reason, message):
        return {"status": status, "reason": reason, "message": message}

    if query.issues:
        return stop("clarification", "ambiguous_century", "短年份的世纪不明确，请使用四位年份。")
    if query.domain == "mixed":
        return stop("clarification", "mixed_domains", "请将文档问题和行情问题分开提问。")
    if query.domain == "market" and FORECAST.search(query.effective):
        return stop("refused", "forecast", "现有数据不能支持股价预测或买卖建议。")
    if "operating_revenue_total" in query.metrics:
        return stop("unsupported", "unreviewed_metric",
                    "当前未核准“营业总收入”指标；它与“营业收入”不同，不能相互替代。")
    if "总收入" in query.effective:
        return stop("clarification", "ambiguous_metric",
                    "请明确“总收入”指营业收入还是营业总收入；当前仅核准营业收入。")
    if query.domain in {"document", "market"} and EXTREME.search(query.effective):
        if not query.resolved["stock_codes"]:
            return stop("clarification", "missing_company", "请指定公司或股票代码。")
        scope = "、".join(query.years) + "年" if query.years else "指定区间"
        message = (f"当前不支持查询{scope}的最高或最低行情值，不能用最新报价代替。"
                   "支持最新股价、PE、PB、市值及最近交易日的前复权日K。"
                   if query.domain == "market" else
                   "当前未实现该财务指标的最高或最低值查询，请明确公司和单个年度的指标。")
        return stop("unsupported", "extreme_not_implemented", message)
    if query.domain == "market" and (query.years or DURATION.search(query.effective)):
        return stop("unsupported", "historical_market_not_implemented",
                    "支持最新行情及最近交易日的前复权日K；不支持指定年度或历史区间行情查询。")
    return None


def market_unrecognized(text: str) -> str:
    rest = text
    for alias in sorted(ALIAS_TO_CODE, key=len, reverse=True):
        rest = rest.replace(alias, "")
    rest = re.sub(r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)", "", rest, flags=re.I)
    rest = re.sub(r"\d+(?:个)?(?:交易)?日", "", rest)
    rest = MARKET.sub("", rest)
    rest = re.sub(
        r"收盘|收市|最新|实时|当前|今日|今天|当日|现|最近|前复权|涨跌幅|涨跌|收益率|收益"
        r"|多少钱|是多少|多少|报价|收盘价|收市价|请问|请|查询|查一下|告诉我|帮我|给我"
        r"|我问的是|问的是|那么|那|它的|的|和|及|与|、|呢|吗|是|为|报", "", rest,
        flags=re.I,
    )
    return re.sub(r"[\s\W_]", "", rest)
