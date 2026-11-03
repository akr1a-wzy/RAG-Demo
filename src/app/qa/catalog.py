"""已复核PDF字段查询；确定性回答/计算，不把目录命中冒充向量检索或LLM生成。"""

import hashlib
import json
import re
import unicodedata
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymupdf

from app.qa.query_understanding import (
    DOCUMENT_ALIASES,
    QueryUnderstanding,
    capability_decision,
    metric_matches,
    understand_query,
)
from app.tools.ticker import ALIAS_TO_CODE, CODE_TO_CANONICAL_NAME

VERSION = "reviewed-document-catalog-v3-query-understanding"
LABELS = {
    "revenue": "营业收入",
    "net_profit_parent": "归母净利润",
    "net_profit_parent_excl": "扣非归母净利润",
    "operating_cashflow": "经营活动现金流量净额",
    "total_assets": "总资产",
    "net_assets_parent": "归母净资产",
    "basic_eps": "基本每股收益",
    "roe": "加权平均净资产收益率",
    "dividend_gross": "现金分红",
    "dividend_policy": "利润分配安排",
    "record_date": "股权登记日",
    "ex_dividend_date": "除权除息日",
    "payment_date": "现金红利发放日",
    "meeting_date": "股东会日期",
}

ALIASES = DOCUMENT_ALIASES
MONEY = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
}
BASES = {"as_reported": "原披露", "adjusted": "调整后", "restated": "重述后"}
STAGES = {"plan": "预案列示", "implementation": "本次实施", "meeting": "会议公告"}


def compact(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).replace("−", "-")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def number(value):
    return Decimal(value.replace(",", "").rstrip("%"))


def verify_facts(root: Path, facts: list[dict], documents: dict) -> dict:
    """逐证据矩形重读；PDF/图像SHA、年份/数值/单位、复核状态均失败关闭。"""
    errors, seen, handles, checked_pdfs, checked_images = [], set(), {}, set(), set()
    try:
        for fact in facts:
            key = fact["fact_id"]
            try:
                if key in seen:
                    raise ValueError("duplicate_fact")
                seen.add(key)
                if fact["review_status"] != "assistant_visual_verified":
                    raise ValueError("not_reviewed")
                d = documents[fact["document_id"]]
                if d["stock_code"] != fact["stock_code"]:
                    raise ValueError("wrong_company")
                path = root / d["local_path"]
                if str(path) not in checked_pdfs:
                    if sha(path) != d["sha256"]:
                        raise ValueError("changed_pdf")
                    checked_pdfs.add(str(path))
                if fact["pdf_sha256"] != d["sha256"]:
                    raise ValueError("unbound_pdf")
                for visual in fact["visual_evidence"]:
                    image_path = root / visual["path"]
                    ik = str(image_path), visual["sha256"]
                    if ik not in checked_images:
                        if sha(image_path) != visual["sha256"]:
                            raise ValueError("changed_visual")
                        checked_images.add(ik)
                if not fact["visual_evidence"]:
                    raise ValueError("missing_visual")
                if fact["document_id"] not in handles:
                    handles[fact["document_id"]] = pymupdf.open(path)
                pdf = handles[fact["document_id"]]
                proofs = {}
                for p in fact["proofs"]:
                    box = pymupdf.Rect(p["bbox"])
                    if box.is_empty or not pdf[p["page"] - 1].rect.contains(box):
                        raise ValueError("invalid_bbox")
                    actual = pdf[p["page"] - 1].get_textbox(box)
                    if compact(actual) != compact(p["text"]):
                        raise ValueError("changed_proof_text")
                    proofs[p["kind"]] = p
                if compact(fact["row_quote"]) != compact(proofs["row"]["text"]):
                    raise ValueError("unbound_quote")
                if fact["metric"] in MONEY_METRICS:
                    if not {"year", "unit", "value"} <= proofs.keys():
                        raise ValueError("missing_column_proof")
                    if compact(proofs["year"]["text"]) != fact["period"]:
                        raise ValueError("wrong_year")
                    vb, yb = proofs["value"]["bbox"], proofs["year"]["bbox"]
                    if abs((vb[0] + vb[2] - yb[0] - yb[2]) / 2) > 45:
                        raise ValueError("wrong_year_column")
                    rb = proofs["row"]["bbox"]
                    if not rb[1] <= (vb[1] + vb[3]) / 2 <= rb[3]:
                        raise ValueError("wrong_row")
                    # 同坐标重复绘制的同值可保留；邻行不同数字一律失败。
                    raws = re.findall(r"\(?[-+]?\d[\d,]*(?:\.\d+)?%?\)?", proofs["value"]["text"])
                    values = [number("-" + v[1:-1] if v.startswith("(") else v) for v in raws]
                    if not values or any(v != number(fact["value"]) for v in values):
                        raise ValueError("wrong_value")
                    unit_text = compact(proofs["unit"]["text"])
                    marker = (
                        "%"
                        if fact["metric"] == "roe"
                        else "元"
                        if fact["metric"] == "basic_eps"
                        else fact["unit"]
                    )
                    if marker not in unit_text:
                        raise ValueError("wrong_unit")
                    if fact["accounting_basis"] != "as_reported":
                        p = proofs.get("accounting_basis", {})
                        expected = "调整后" if fact["accounting_basis"] == "adjusted" else "重述后"
                        if compact(p.get("text", "")) != expected:
                            raise ValueError("missing_basis")
                elif fact["metric"].endswith("_date"):
                    if compact(fact["raw_value"]) not in compact(fact["row_quote"]):
                        raise ValueError("unbound_date")
                    y, m, d = map(int, re.findall(r"\d+", fact["raw_value"])[:3])
                    if date(y, m, d).isoformat() != fact["value"]:
                        raise ValueError("wrong_date")
                elif fact["metric"] == "dividend_gross":
                    if compact(fact["value"]) not in compact(fact["row_quote"]):
                        raise ValueError("unbound_dividend")
                    quote = compact(fact["row_quote"])
                    basis = 10 if "每10股" in quote else 1
                    if basis != fact["share_basis"] or "含税" not in quote:
                        raise ValueError("wrong_dividend_basis")
            except (ValueError, KeyError, OSError, IndexError) as error:
                errors.append({"fact_id": key, "reason": str(error)})
    finally:
        for pdf in handles.values():
            pdf.close()
    return {
        "facts": len(facts),
        "pdfs": len(checked_pdfs),
        "visuals": len(checked_images),
        "errors": errors,
        "passed": not errors,
    }


MONEY_METRICS = set(LABELS) - {
    "dividend_gross",
    "dividend_policy",
    "record_date",
    "ex_dividend_date",
    "payment_date",
    "meeting_date",
}


def parse_request(question, history, *, understanding: QueryUnderstanding | None = None):
    query = understanding or understand_query(question, history)
    text = compact(query.effective)
    for code in CODE_TO_CANONICAL_NAME:
        text = re.sub(rf"(?<!\d){code}(?=20\d{{2}}年)", code + " ", text)
    resolved = {**query.resolved, "query_understanding": query.metadata()}
    matches = metric_matches(text)
    metrics = list(dict.fromkeys(m[2] for m in sorted(matches)))
    overview = bool(re.search(r"财务概览|主要财务(?:数据|指标)|八项财务", text))
    if overview:
        metrics = sorted(MONEY_METRICS)
    if not metrics:
        return None
    years = list(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)))
    if not years:
        years = [resolved["report_period"]] if resolved["report_period"] else []
    resolved["report_period"] = years[0] if years else None
    ann = any(m not in MONEY_METRICS for m in metrics)
    change = bool(re.search(r"同比|增长|增减|变化|变动", text))
    difference = bool(re.search(r"差多少|相差|差额|百分点", text))
    years = ["2025", "2024"] if change and years == ["2025"] else years
    stages = [
        s
        for s, p in [
            ("plan", r"预案|计划|拟派"),
            ("implementation", r"实施|实际派"),
            ("meeting", r"股东(?:大)?会"),
        ]
        if re.search(p, text)
    ]
    units = re.findall(r"亿元|百万元|万元|千元|(?<!每股)(?<![百千万亿])元", text)
    unit = units[0] if units else None
    # 不删除未知限定后假装识别成功；残留内容必须明确属于常用提问语法。
    rest = text
    for a, b, _ in sorted(matches, reverse=True):
        rest = rest[:a] + " " * (b - a) + rest[b:]
    for alias in sorted(ALIAS_TO_CODE, key=len, reverse=True):
        rest = rest.replace(alias, "")
    rest = re.sub(
        r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)|20\d{2}|亿元|百万元|万元|千元|元", "", rest, flags=re.I
    )
    rest = re.sub(
        r"财务概览|主要财务数据|主要财务指标|八项财务|换算|转换|折合|按|用|人民币"
        r"|告诉我|帮我|请问|麻烦|查询|查一下|查查|给我|分别|列出|列示|展示|比较"
        r"|对比|相比|相差|差多少|差额|同比|增长|增减|变化|变动|百分点|保持|沿用"
        r"|还是|继续|那|它们|它|这家|公司|集团|体系|合并口径|合并|口径|原文|来源"
        r"|证据|物理页|页码|引用|原始|原值|原单位|是多少|多少|是|什么|时候|日期|时间"
        r"|以及|还有|和|及|与|并|的|呢|吗|啊|了|年末|年度|年报|报告期|年|末|情况|数据"
        r"|怎么样|如何|各自|各|两家|两年|这两年|同时|再|一下|看下|看|以|为|到|成"
        r"|预案|计划|拟派|实施|实际派|A股|税前|含税|本次|现金|所有|全部|不|是否|会"
        r"|股东大会|股东会|召开|公告|每股|每10股|十|每|股|总览|明细|请|附",
        "",
        rest,
        flags=re.I,
    )
    rest = re.sub(r"[\s\W_]", "", rest)
    for condition in query.constraints:
        if rest and rest in condition:
            rest = condition
    distribution_scope = (
        "second_half" if "下半年" in text else "full_year" if "全年" in text else None
    )
    if ann and distribution_scope:
        rest = rest.replace("下半", "").replace("全", "").replace("累计", "")
    scope_text = text.replace("下半年", "") if ann else text
    blocked = bool(rest) or bool(
        re.search(
            r"母公司(?:口径|自身|单体)|子公司|季度|半年|Q[1-4]|H[12]|美元|港元|不含|剔除"
            r"|税后|扣税|预测|明年|下个?月|明天|未来|一定|原因|主营|营业总收入|稀释"
            r"|调整前|重述前",
            scope_text,
            re.I,
        )
    )
    blocked |= ann and unit not in {None, "元"}
    decision = capability_decision(query)
    blocked |= decision is not None
    resolved["query_understanding"]["unrecognized_conditions"] = rest
    if decision is not None:
        resolved["query_understanding"]["capability"] = decision
    return {
        "resolved": resolved,
        "metrics": metrics,
        "years": years,
        "announcement": ann,
        "change": change,
        "difference": difference,
        "unit": unit,
        "stages": stages,
        "distribution_scope": distribution_scope,
        "share_basis": 10 if re.search(r"每(?:10|十)股", text) else 1 if "每股" in text else None,
        "status": "unsupported_conditions" if blocked else "supported",
        "unrecognized": rest,
    }


class DocumentCatalog:
    def __init__(self, root: Path):
        self.root = root
        release = json.loads((root / "data/qa/catalog_release.v2.json").read_text(encoding="utf-8"))
        self.documents = {
            d["document_id"]: d
            for d in map(
                json.loads,
                (root / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines(),
            )
        }
        self.facts = []
        for item in release["catalogs"]:
            if sha(root / item["path"]) != item["sha256"]:
                raise ValueError("Changed catalog")
            self.facts.extend(
                json.loads((root / item["path"]).read_text(encoding="utf-8"))["facts"]
            )
        self.audit = verify_facts(root, self.facts, self.documents)
        if not self.audit["passed"]:
            raise ValueError("Catalog proof validation failed: " + str(self.audit["errors"][:3]))

    def coverage(self):
        return {
            code: {
                "stock_name": CODE_TO_CANONICAL_NAME[code],
                "financial_years": ["2025", "2024"],
                "financial_metrics": [LABELS[m] for m in sorted(MONEY_METRICS)],
                "announcement_fields": sorted(
                    {
                        LABELS[f["metric"]]
                        for f in self.facts
                        if f["stock_code"] == code and f["metric"] not in MONEY_METRICS
                    }
                ),
            }
            for code in CODE_TO_CANONICAL_NAME
        }

    def ask(self, question, history, *, evidence_only=False,
            understanding: QueryUnderstanding | None = None):
        query = understanding or understand_query(question, history)
        request = parse_request(question, history, understanding=query)
        if request is None:
            return None
        resolved = request["resolved"]
        codes, years = resolved["stock_codes"], request["years"]
        common = {
            "resolved": {**resolved, "catalog_request": request},
            "sources": [],
            "deployment": {"document_catalog_version": VERSION},
            "usage": {},
            "generated_answer_accuracy_accepted": False,
            "answer_method": "structured_facts",
        }

        def stop(mode, message):
            return {**common, "mode": mode, "message": message}

        decision = capability_decision(query)
        if decision is not None:
            return stop(decision["status"], decision["message"])
        if not codes:
            return stop("clarification", "请指定公司或股票代码；裸称平安需明确主体。")
        if not set(codes) <= CODE_TO_CANONICAL_NAME.keys():
            return stop("refused", "当前文档目录不覆盖该公司。")
        if not years:
            return stop("clarification", "请指定报告年度，例如2025年。")
        if request["status"] != "supported":
            return stop(
                "unsupported",
                (f"未识别或未核准的条件：{request['unrecognized'][:100]}。请明确指标和口径。"
                 if request["unrecognized"] else
                 "该指标或限定条件尚未核准，请查看字段覆盖清单。"),
            )
        if not set(years) <= {"2025", "2024"}:
            return stop("refused", "已核准财务字段覆盖2025年和年报列示的2024年比较数。")
        if request["announcement"] and (
            years != ["2025"]
            or any(m in MONEY_METRICS for m in request["metrics"])
            or request["change"]
        ):
            return stop(
                "unsupported",
                "公告请按2025年度分别查询，发生日期按原文列示。财务与公告请分开提问。",
            )
        selected, missing = [], []
        for code in codes:
            for metric in request["metrics"]:
                for year in years:
                    fs = [
                        f
                        for f in self.facts
                        if f["stock_code"] == code and f["metric"] == metric and f["period"] == year
                    ]
                    if request["stages"]:
                        fs = [f for f in fs if f.get("event_stage") in request["stages"]]
                    if request["distribution_scope"]:
                        fs = [
                            f
                            for f in fs
                            if f.get("distribution_scope") == request["distribution_scope"]
                        ]
                    if (
                        not fs
                        and metric == "dividend_gross"
                        and (not request["stages"] or request["stages"] == ["plan"])
                    ):
                        fs = [
                            f
                            for f in self.facts
                            if f["stock_code"] == code and f["metric"] == "dividend_policy"
                        ]
                    if not fs:
                        missing.append(f"{CODE_TO_CANONICAL_NAME[code]}{year}年{LABELS[metric]}")
                    selected.extend(fs)
        if missing:
            return stop(
                "refused",
                "当前原文目录缺少已核准字段：" + "、".join(missing) + "。不据此推断为零或未发生。",
            )
        sources, source_map = [], {}
        for f in selected:
            key = f["document_id"], f["physical_page"]
            if key not in source_map:
                sid = f"S{len(sources) + 1}"
                d = self.documents[f["document_id"]]
                source_map[key] = sid
                sources.append(
                    {
                        "source_id": sid,
                        "document_id": d["document_id"],
                        "stock_code": d["stock_code"],
                        "stock_name": d["stock_name"],
                        "report_period": d["report_period"],
                        "title": d["title"],
                        "document_type": d["document_type"],
                        "section": "已核准字段",
                        "physical_page": f["physical_page"],
                        "source_url": d["source_url"],
                        "content": "",
                        "facts": [],
                        "layout_review_required": True,
                        "selected_cells_reviewed": True,
                    }
                )
            source = next(s for s in sources if s["source_id"] == source_map[key])
            source["facts"].append(f)
            # 跨页表头另标原物理页，引用仍使用当前事实所在页。
            proof_text = "\n".join(dict.fromkeys(p["text"].strip() for p in f["proofs"]))
            source["content"] += f"\n[字段{f['fact_id']}]\n{proof_text}\n"
        common["sources"] = sources
        common["fact_selection"] = {
            "status": "ready",
            "method": "reviewed_catalog_lookup",
            "fact_ids": [f["fact_id"] for f in selected],
        }
        common["validation"] = {
            "checks_passed": True,
            "catalog_proofs_verified": True,
            "facts": len(selected),
            "errors": [],
        }
        if evidence_only:
            return {
                **common,
                "mode": "evidence_only",
                "message": "仅展示已核准原文证据，未调用生成接口。",
            }

        def cite(f):
            return {
                "source_id": source_map[f["document_id"], f["physical_page"]],
                "quote": f["row_quote"],
            }

        claims = []
        for f in selected:
            name = CODE_TO_CANONICAL_NAME[f["stock_code"]]
            if f["metric"] in MONEY_METRICS:
                value, unit = number(f["value"]), f["unit"]
                converted = ""
                if request["unit"] and unit in MONEY:
                    value = value * MONEY[unit] / MONEY[request["unit"]]
                    unit = request["unit"]
                    converted = f"（原值{f['value']}{f['unit']}）"
                elif request["unit"] and unit not in MONEY:
                    return stop("unsupported", "每股收益和百分比不按金额单位换算，请分别查询。")
                basis = (
                    f"（2025年报{BASES[f['accounting_basis']]}比较数）"
                    if f["period"] == "2024"
                    else ""
                )
                text = (
                    f"{name}{f['period']}年{LABELS[f['metric']]}为"
                    f"{format(value, 'f')}{unit}{converted}{basis}。"
                )
            elif f["metric"] == "dividend_gross":
                share_basis = request["share_basis"] or f["share_basis"]
                amount = format(number(f["value"]) * share_basis / f["share_basis"], "f")
                text = (
                    f"{name}2025年度相关公告：{STAGES[f['event_stage']]}"
                    f"每{share_basis}股派{amount}元（含税）。该数为该方案列值。"
                )
                if share_basis != f["share_basis"]:
                    text += f"按原值每{f['share_basis']}股{f['value']}元折算。"
                if f.get("scope_label"):
                    text += f"口径：{f['scope_label']}。"
            elif f["metric"] == "dividend_policy":
                text = f"{name}2025年度预案列示：{f['value']}。"
            else:
                text = (
                    f"{name}2025年度相关{STAGES[f['event_stage']]}："
                    f"{LABELS[f['metric']]}为{f['value']}。"
                )
            claims.append(
                {
                    "text": text,
                    "stock_code": f["stock_code"],
                    "report_period": f["period"],
                    "fact_ids": [f["fact_id"]],
                    "citations": [cite(f)],
                }
            )
        calculations = []
        if request["change"] or request["difference"]:
            for code in codes:
                for metric in request["metrics"]:
                    fs = [f for f in selected if f["stock_code"] == code and f["metric"] == metric]
                    by_year = {f["period"]: f for f in fs}
                    if set(by_year) != {"2025", "2024"}:
                        continue
                    a, b = by_year["2025"], by_year["2024"]
                    av, bv = number(a["value"]), number(b["value"])
                    if a["unit"] != b["unit"]:
                        av *= MONEY[a["unit"]]
                        bv *= MONEY[b["unit"]]
                    if metric == "roe":
                        val = av - bv
                        formula = "2025数值-2024数值"
                        suffix = "个百分点"
                    elif request["difference"] and not request["change"]:
                        val = av - bv
                        formula = "2025数值-2024数值"
                        suffix = a["unit"]
                        if request["unit"] and suffix in MONEY:
                            val = val * MONEY[suffix] / MONEY[request["unit"]]
                            suffix = request["unit"]
                    elif bv <= 0:
                        claims.append(
                            {
                                "text": (
                                    f"{CODE_TO_CANONICAL_NAME[code]}{LABELS[metric]}"
                                    "2024年基数非正，不报告常规同比百分比；请比较原值。"
                                ),
                                "stock_code": code,
                                "report_period": "2025",
                                "citations": [cite(a), cite(b)],
                            }
                        )
                        continue
                    else:
                        val = (av / bv - 1) * 100
                        formula = "(2025数值/2024数值-1)*100"
                        suffix = "%"
                    value = str(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    calculations.append(
                        {
                            "stock_code": code,
                            "metric": metric,
                            "formula": formula,
                            "inputs": [a["fact_id"], b["fact_id"]],
                            "result": value,
                            "unit": suffix,
                        }
                    )
                    claims.append(
                        {
                            "text": f"{CODE_TO_CANONICAL_NAME[code]}2025年{LABELS[metric]}较2024年"
                            f"{BASES[b['accounting_basis']]}数变化{value}{suffix}（按披露数计算，四舍五入到两位）。",
                            "stock_code": code,
                            "report_period": "2025",
                            "citations": [cite(a), cite(b)],
                        }
                    )
        if request["difference"] and len(codes) == 2 and len(years) == 1:
            for metric in request["metrics"]:
                a, b = [
                    next(f for f in selected if f["stock_code"] == c and f["metric"] == metric)
                    for c in codes
                ]
                av, bv = number(a["value"]), number(b["value"])
                unit = a["unit"]
                if unit in MONEY:
                    unit = request["unit"] or "亿元"
                    av = av * MONEY[a["unit"]] / MONEY[unit]
                    bv = bv * MONEY[b["unit"]] / MONEY[unit]
                val = str((av - bv).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                if metric == "roe":
                    unit = "个百分点"
                calculations.append(
                    {
                        "metric": metric,
                        "formula": "第一家公司减第二家公司",
                        "inputs": [a["fact_id"], b["fact_id"]],
                        "result": val,
                        "unit": unit,
                    }
                )
                claims.append(
                    {
                        "text": f"{CODE_TO_CANONICAL_NAME[codes[0]]}减"
                        f"{CODE_TO_CANONICAL_NAME[codes[1]]}的{years[0]}年{LABELS[metric]}差额为{val}{unit}。",
                        "stock_code": codes[0],
                        "report_period": years[0],
                        "citations": [cite(a), cite(b)],
                    }
                )
        common["validation"]["calculations"] = calculations
        return {
            **common,
            "mode": "structured_answer",
            "claims": claims,
            "answer_method": "verified_calculation" if calculations else "structured_facts",
            "message": ("按年报“营业收入”口径列示；“营业总收入”为另一指标。"
                        if any(n["kind"] == "revenue_alias" for n in query.normalizations) else "")
            + "依据已核准原文字段直接列示；计算使用披露值，未调用生成接口。",
        }
