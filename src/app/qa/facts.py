"""保守的行/年度列/数值/单位绑定；不从扁平文本猜测复杂合并表头。"""

import hashlib
import re
import unicodedata
from decimal import Decimal

VERSION = "local-row-year-unit-v5-reviewed-financial"
NUMBER = r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?"
UNIT = r"万亿元|百万元|亿元|万元|千元|元/股|元|万人|人|名|万股|股|%"
CELL = re.compile(rf"(?P<value>{NUMBER})\s*(?P<unit>{UNIT})?")
YEAR_HEADER = re.compile(r"20\d{2}\s*年(?:末|度)?")
CLAIM_DATE = re.compile(r"(20\d{2})(?:年|[-/.])(\d{1,2})(?:月|[-/.])(\d{1,2})日?")
EMPLOYEE_HEADINGS = {
    "员工情况", "公司员工情况", "本公司员工情况", "在职员工情况",
    "报告期末员工情况", "报告期末母公司和主要子公司的员工情况",
    "报告期末母公司和主要子公司员工情况", "员工数量、专业构成及教育程度",
}
# 只列已支持的指标；不以任意包含“收入/员工”的句子作为指标行。
METRICS = {
    "headcount_total": ("在职员工的数量合计", "在职员工数量合计", "在职员工总人数",
                        "在职员工数量", "在职员工人数", "员工数量", "员工人数",
                        "员工总数", "员工总人数", "在职员工总数"),
    "headcount_parent": ("母公司在职员工的数量", "母公司在职员工数量", "母公司员工人数",
                         "本公司总部在职员工的数量"),
    "headcount_subsidiaries": ("主要子公司在职员工的数量", "主要子公司在职员工数量",
                               "本公司子分公司在职员工的数量"),
    "net_profit_parent_excl": ("归属于本公司股东的扣除非经常性损益的净利润",
                               "归属于上市公司股东的扣除非经常性损益的净利润"),
    "net_profit_parent": ("归属于本公司股东的净利润", "归属于上市公司股东的净利润",
                          "归母净利润"),
    "net_assets_parent": ("归属于本公司股东的净资产", "归属于上市公司股东的净资产"),
    "operating_cashflow": ("经营活动产生的现金流量净额",),
    "total_profit": ("利润总额",),
    "total_assets": ("资产总计", "资产总额", "总资产"),
    "revenue": ("营业收入", "营收"),
    "total_revenue": ("营业总收入",),
}


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def decimal_text(value: str) -> str:
    return str(Decimal(value.replace(",", "")).normalize())


def calendar_dates(text: str) -> set[tuple[str, int, int]]:
    return {(m[1], int(m[2]), int(m[3])) for m in CLAIM_DATE.finditer(compact(text))}


def metric_in(text: str) -> str | None:
    text = compact(text)
    if re.search(r"合计|总计|总数|总人数", text):
        text = re.sub(r"母公司(?:和|及|与)主要子公司(?:的)?", "本集团", text)
    matches = [(alias, key) for key, aliases in METRICS.items()
               for alias in aliases if alias in text]
    narrative = re.search(r"(?:本集团|本公司|体系内)(?:共有|拥有)(?:在职)?员工", text)
    if narrative:
        matches.append((narrative[0], "headcount_total"))
    maximal = {(key, alias) for alias, key in matches
               if not any(alias != other and alias in other for other, _ in matches)}
    keys = {key for key, _ in maximal}
    if len(keys) != 1:
        return None
    key = keys.pop()
    if key == "headcount_total" and re.search(r"母公司|子公司|总部|退休|离职", text):
        return None
    return key


def row_metric(label: str) -> str | None:
    label = compact(label)
    label = re.sub(rf"\((?:{UNIT})\)$", "", label)
    label = re.sub(r"^报告期末", "", label)
    label = re.sub(r"^(?:本集团|集团)", "", label)
    return next((key for key, aliases in METRICS.items() if label in aliases), None)


def employee_heading_years(text: str) -> list[str] | None:
    """仅识别完整员工标题；含“员工情况”的普通句子不能重置年度保护。"""
    title = compact(text)
    title = re.sub(r"^(?:[一二三四五六七八九十百]+[、.]|\([一二三四五六七八九十0-9]+\)"
                   r"|\d+[、.])", "", title)
    years = re.findall(r"20\d{2}", title)
    title = re.sub(r"(?:截至)?20\d{2}年(?:末|度)?(?:的)?", "", title)
    return years if title in EMPLOYEE_HEADINGS else None


def employee_metadata_scope(section: str, period: str) -> tuple[bool, bool, str | None]:
    """汇总section可跨多个章节；只从员工标题继承显式年度，不读取其他章节的年数。"""
    historical = False
    quote = None
    for part in section.split("|"):
        years = employee_heading_years(part)
        if years is not None:
            if years:
                historical = set(years) != {period}
            if not historical:
                quote = part.strip()
        elif "员工" in part:
            # 未识别但带历史年度的员工标题仍保守隔离，不能用通用子标题洗掉。
            unknown_years = re.findall(r"20\d{2}", part)
            if any(year != period for year in unknown_years):
                historical = True
    return "员工" in section, historical, quote


def narrative_employee_facts(source: dict, body: str) -> list[dict]:
    """仅解析带明确时点、主体和计数单位的员工总数句，不抽取退休/专业人数。"""
    if source.get("document_type") != "annual_report":
        return []
    section = source.get("section", "")
    if "子公司" in section and not re.search(r"母公司.*(?:和|及).*子公司", section):
        return []
    chars, offsets = [], []
    for index, char in enumerate(body):
        for normalized in unicodedata.normalize("NFKC", char):
            if not normalized.isspace():
                chars.append(normalized)
                offsets.append(index)
    text = "".join(chars)
    name = re.escape(source.get("stock_name", ""))
    short = re.escape(source.get("stock_name", "").removeprefix("中国"))
    subject = rf"本集团|本公司|(?:{name}|{short})体系内" if name else r"本集团|本公司"
    pattern = re.compile(
        rf"(?P<date>截至报告期末|(?:截至|于)(?P<year>20\d{{2}})年(?:12月31日|末))[,，]?"
        rf"(?P<subject>{subject})(?:共有|拥有)(?:在职)?员工"
        rf"(?P<value>{NUMBER})(?P<unit>人|名)(?:\d{{1,3}})?"
        rf"(?:\((?P<qualifier>含[^()。;；]{{1,30}})\))?"
    )
    facts = []
    for match in pattern.finditer(text):
        prefix = re.split(r"[。;；!?！？]", text[:match.start()])[-1][-24:]
        if re.search(r"预计|计划|将于|如果|假设", prefix):
            continue
        count = Decimal(match["value"].replace(",", ""))
        if count < 0 or count % 1:
            continue
        start, end = offsets[match.start()], offsets[match.end() - 1] + 1
        if not match["year"]:
            _, historical, _ = employee_metadata_scope(section, source["report_period"])
            for previous in body[:start].splitlines():
                years = employee_heading_years(previous)
                if years:
                    historical = set(years) != {source["report_period"]}
                elif years is None and "员工" in previous and any(
                    y != source["report_period"] for y in re.findall(r"20\d{2}", previous)
                ):
                    historical = True
            if historical:
                continue
        quote = body[start:end]
        subject_text = match["subject"]
        scope = ("system" if "体系内" in subject_text else
                 "group" if subject_text == "本集团" else "filing_company")
        fact = {
            "metric": "headcount_total",
            "row_label": text[match.start("subject"):match.start("value")],
            "period": match["year"] or source["report_period"], "value": match["value"],
            "unit": "人", "unit_basis": "explicit_person_count_synonym",
            "raw_unit": match["unit"], "unit_quote": quote, "entity_scope": scope,
            "scope_qualifier": match["qualifier"],
            "year_basis": "explicit_narrative_date" if match["year"] else "explicit_report_end",
            "year_context_quote": quote, "year_context_origin": "body_narrative_date",
            "column_index": None, "header_quote": None, "row_quote": quote,
            "line_number": body[:start].count("\n") + 1,
            "stock_code": source["stock_code"], "source_id": source["source_id"],
            "document_id": source.get("document_id"), "physical_page": source.get("physical_page"),
            "version": VERSION,
        }
        fact["fact_id"] = hashlib.sha256(str(fact).encode()).hexdigest()[:20]
        facts.append(fact)
    return facts


def source_facts(source: dict, review_provider=None) -> list[dict]:
    """仅接受明确单行指标、等宽唯一年度列；单位/年度不跨小节继承。

    员工数量的“人”是带标记的计数语义，不伪造成原文字面单位。
    财务数字必须有局部显式单位和年度列，不能用报告元数据代替列绑定。
    """
    reviewed = review_provider.facts_for(source) if review_provider else []
    if source.get("layout_review_required"):
        return reviewed
    body = re.split(r"正文[:：]", source["content"], maxsplit=1)[-1]
    facts = []
    header, local_unit, local_unit_quote = None, None, None
    employee_section, historical_employee_heading, year_context = employee_metadata_scope(
        source.get("section", ""), source["report_period"],
    )
    implicit_employee_year = not historical_employee_heading
    year_context_origin = "section_employee_heading"
    for line_number, original in enumerate(body.splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        context_years = employee_heading_years(line)
        if context_years is not None:
            employee_section = True
            if context_years:
                implicit_employee_year = set(context_years) == {source["report_period"]}
                historical_employee_heading = not implicit_employee_year
            elif not historical_employee_heading:
                # 明确的新员工小节可以隔离此前无关段落里的历史年份。
                implicit_employee_year = True
            year_context, year_context_origin = line, "body_employee_heading"
            header, local_unit, local_unit_quote = None, None, None
            continue
        unit_match = re.fullmatch(rf"单位\s*[:：]\s*({UNIT})(?:\s*[币种：:人民币]*)?", line)
        if unit_match:
            local_unit, header = unit_match[1], None
            local_unit_quote = line
            continue
        # 多次出现同年、增减列、重述多级表头不能在失去坐标后推测列跨度。
        if YEAR_HEADER.search(line) and not re.search(rf"(?:{NUMBER})\s*(?:{UNIT})$", line):
            years = YEAR_HEADER.findall(line)
            residue = YEAR_HEADER.sub("", re.sub(r"^(?:项目|指标)\s*", "", line))
            normalized = [re.search(r"20\d{2}", y)[0] for y in years]
            if not compact(residue) and len(set(normalized)) == len(normalized):
                header = {"years": normalized, "quote": line}
                continue
            # 含年份的指标行会在下面处理，其他年份文字是上下文边界。
        explicit_year = re.match(r"^(20\d{2})\s*年(?:末|度)?\s*", line)
        candidate = line[explicit_year.end():] if explicit_year else line
        if explicit_year and explicit_year[1] != source["report_period"]:
            # 即使剥去年份后没有数字单元格，也不能跳过历史年度保护。
            implicit_employee_year = False
            if "员工" in candidate:
                historical_employee_heading = True
        first = re.search(NUMBER, candidate)
        if first is None:
            header, local_unit = None, None
            continue
        label = candidate[:first.start()].strip().rstrip(":：为是")
        metric = row_metric(label)
        tail = candidate[first.start():]
        cells = list(CELL.finditer(tail))
        residue = CELL.sub("", tail)
        if not metric or not cells or compact(residue):
            if any(y != source["report_period"] for y in re.findall(r"20\d{2}", line)):
                implicit_employee_year = False
                if "员工" in line:
                    historical_employee_heading = True
            header, local_unit = None, None
            continue
        if explicit_year:
            years, year_basis = [explicit_year[1]], "explicit_row_year"
        elif header:
            years, year_basis = header["years"], "explicit_year_columns"
        elif (implicit_employee_year and metric.startswith("headcount_") and len(cells) == 1
              and source.get("document_type") == "annual_report"
              and employee_section):
            years, year_basis = [source["report_period"]], "annual_employee_section_report_period"
        else:
            continue
        if len(years) != len(cells):
            continue
        for column, (year, cell) in enumerate(zip(years, cells, strict=True), 1):
            explicit_unit = cell["unit"] or next(
                iter(re.findall(rf"\(({UNIT})\)", compact(label))), None
            )
            unit = explicit_unit or local_unit
            unit_basis = "row_explicit" if explicit_unit else "local_table_unit"
            if unit == "名":
                unit, unit_basis = "人", "explicit_person_count_synonym"
            if unit is None and metric.startswith("headcount_"):
                unit, unit_basis = "人", "employee_count_semantics"
            if unit is None:
                continue
            # 不让货币表的单位污染员工行，不让比例/每股值借用总金额单位。
            if metric.startswith("headcount_") and unit not in {"人", "万人"}:
                continue
            if unit == "人" and Decimal(cell["value"].replace(",", "")) % 1:
                continue
            if (not metric.startswith("headcount_")
                    and unit not in {"元", "千元", "万元", "百万元", "亿元", "万亿元"}):
                continue
            fact = {
                "metric": metric, "row_label": label, "period": year,
                "value": cell["value"], "unit": unit, "unit_basis": unit_basis,
                "unit_quote": line if explicit_unit else local_unit_quote if local_unit else None,
                "year_basis": year_basis, "column_index": column,
                "year_context_quote": year_context if year_basis ==
                                      "annual_employee_section_report_period" else None,
                "year_context_origin": year_context_origin if year_basis ==
                                       "annual_employee_section_report_period" else None,
                "header_quote": header["quote"] if header and not explicit_year else None,
                "row_quote": line, "line_number": line_number,
                "stock_code": source["stock_code"], "source_id": source["source_id"],
                "document_id": source.get("document_id"),
                "physical_page": source.get("physical_page"), "version": VERSION,
            }
            identity = f"{source.get('document_id')}:{source.get('physical_page')}:{fact}"
            fact["fact_id"] = hashlib.sha256(identity.encode()).hexdigest()[:20]
            facts.append(fact)
    facts.extend(narrative_employee_facts(source, body))
    facts.extend(reviewed)
    return facts


def bind_claim(
    text: str, period: str, cited: list[tuple[dict, str]], review_provider=None,
) -> tuple[dict | None, str | None]:
    """每条数值陈述只支持一个原始指标；跨行拼数、换算、计算及歧义均拒绝。"""
    metric = metric_in(text)
    if metric is None:
        return None, "unbound_metric"
    dates = calendar_dates(text)
    without_dates = CLAIM_DATE.sub("", compact(text))
    without_year = re.sub(r"20\d{2}\s*年(?:末|度)?", "", without_dates)
    amounts = list(re.finditer(rf"({NUMBER})\s*({UNIT})", without_year))
    # 每个非年份数字都须是唯一的、带单位的值，不能偷偷再叙述另一项事实。
    leftover = re.sub(rf"{NUMBER}\s*(?:{UNIT})", "", without_year)
    if len(amounts) != 1 or re.search(NUMBER, leftover):
        return None, "single_fact_required"
    value, unit = amounts[0][1], amounts[0][2]
    unit = "人" if unit == "名" else unit
    candidates = []
    date_mismatch = False
    scope_mismatch = False
    for source, quote in cited:
        for fact in source_facts(source, review_provider):
            # 引用至少包含同一行的指标和数值；不能用同页另一个数字拼接。
            quote_numbers = {decimal_text(m[0]) for m in re.finditer(NUMBER, quote)}
            if (fact["metric"] == metric and fact["period"] == period
                    and compact(fact["row_label"]) in compact(quote)
                    and decimal_text(fact["value"]) in quote_numbers
                    and (not fact.get("scope_qualifier")
                         or compact(fact["scope_qualifier"]) in compact(quote))):
                scope = fact.get("entity_scope")
                if scope == "consolidated_group" and re.search(
                    r"母公司|子公司|本公司口径|单体|分部|季度|调整|重述|同比|增长|下降"
                    r"|营业总收入", text,
                ):
                    scope_mismatch = True
                    continue
                if ((scope == "system" and re.search(r"母公司|子公司|本集团", text))
                        or (scope != "system" and "体系" in text)
                        or (scope == "filing_company"
                            and re.search(r"母公司|子公司|本集团", text))):
                    scope_mismatch = True
                    continue
                if dates and not dates <= calendar_dates(fact.get("year_context_quote") or ""):
                    date_mismatch = True
                    continue
                candidates.append(fact)
    if not candidates:
        return None, ("entity_scope_mismatch" if scope_mismatch else
                      "unsupported_date" if date_mismatch else "unbound_row_year")
    if len({(decimal_text(f["value"]), f["unit"]) for f in candidates}) != 1:
        return None, "ambiguous_fact"
    fact = candidates[0]
    if unit != fact["unit"]:
        return None, "unsupported_unit"
    if decimal_text(value) != decimal_text(fact["value"]):
        return None, "value_year_column_mismatch"
    return fact, None
