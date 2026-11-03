"""按独立PDF金标准评分，不导入运行时QA校验器或事实目录。"""

import re
import unicodedata
from decimal import Decimal

ANSWER_GROUPS = {"new_staff_single", "staff_comparison", "staff_followup", "financial_answerable"}
STAFF_GROUPS = ANSWER_GROUPS - {"financial_answerable"}
AMOUNT = re.compile(
    r"([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(万亿元|百万元|千元|亿元|万元|元|万人|人|名|%)"
)


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def decimal_value(text):
    return Decimal(text.replace(",", ""))


def proportion(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def claim_matches(claim, fact, company_names):
    if (claim.get("stock_code") != fact["stock_code"]
            or claim.get("report_period") != fact["period"]):
        return False
    text = normalized(claim.get("text", ""))
    years = re.findall(r"(20\d{2})年", text)
    if any(y != fact["period"] for y in years):
        return False
    if any(name in text and code != fact["stock_code"] for code, name in company_names.items()):
        return False
    amounts = AMOUNT.findall(text)
    if len(amounts) != 1:
        return False
    value, unit = amounts[0]
    if (decimal_value(value) != decimal_value(fact["value"])
            or ("人" if unit == "名" else unit) != fact["unit"]):
        return False
    residue = re.sub(r"20\d{2}年\d{1,2}月\d{1,2}日|20\d{2}年(?:末|度)?", "", text)
    residue = AMOUNT.sub("", residue)
    residue = residue.replace("(" + fact["stock_code"] + ")", "")
    if re.search(r"\d", residue):
        return False
    if re.search(r"增长|下降|增加|减少|领先|最多|最少|同比|意味着|排名|高于|低于|差额|翻倍", text):
        return False
    if fact["metric"] == "headcount_total":
        if "员工" not in text or re.search(r"退休|离职|生产人员|学历|研发人员|平均|新招", text):
            return False
        if re.search(r"母公司|子公司|总部", text) and not re.search(r"(?:合计|总计)", text):
            return False
        if re.search(r"收入|利润|资产|薪酬|现金", text):
            return False
    elif not re.search(r"营业收入|营收", text):
        return False
    return True


def citation_score(citation, source, fact, claim_text):
    if source is None:
        return {"literal": False, "supported": False, "reason": "unknown_source"}
    quote = normalized(citation.get("quote", ""))
    literal = bool(quote) and quote in normalized(source.get("content", ""))
    if fact is None:
        return {"literal": literal, "supported": False, "reason": "no_matching_expected_fact"}
    page_ok = (source.get("document_id") == fact["document_id"]
               and source.get("physical_page") == fact["physical_page"])
    label = normalized(fact["row_label"])
    for prefix in ["报告期末", "本集团"]:
        label = label.removeprefix(prefix)
    row_ok = label in quote
    # 字面匹配允许去空白，数值分词必须保留列间空白，防止两列小数粘连。
    numeric_text = unicodedata.normalize("NFKC", citation.get("quote", ""))
    quoted_numbers = re.findall(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?", numeric_text)
    value_ok = any(decimal_value(v) == decimal_value(fact["value"]) for v in quoted_numbers)
    context_ok = all(token in normalized(claim_text) + quote
                     for token in fact.get("required_context", []))
    supported = literal and page_ok and row_ok and value_ok and context_ok
    return {"literal": literal, "supported": supported,
            "reason": "verified_gold_row" if supported else "unverified_page_row_value_or_scope",
            "page_matches_reviewed_gold": page_ok, "row_matches": row_ok,
            "value_matches": value_ok, "context_matches": context_ok}


def score_case(case, response, facts):
    expected = [facts[key] for key in case.get("fact_ids", [])]
    names = {f["stock_code"]: f["stock_name"] for f in facts.values()}
    source_list = response.get("sources", [])
    sources = {s["source_id"]: s for s in source_list}
    duplicate_sources = len(sources) != len(source_list)
    matched = set()
    claims = []
    for claim in response.get("claims", []):
        candidates = [f for f in expected if claim_matches(claim, f, names)]
        fact = candidates[0] if len(candidates) == 1 else None
        citations = [citation_score(c, sources.get(c.get("source_id")), fact,
                                    claim.get("text", "")) for c in claim.get("citations", [])]
        correct = fact is not None and response.get("mode") == "draft"
        if correct:
            matched.add(fact["fact_id"])
        claims.append({"text": claim.get("text", ""), "correct": correct,
                       "matched_fact_id": fact["fact_id"] if fact else None,
                       "citations": citations,
                       "all_citations_supported": bool(citations) and not duplicate_sources
                       and all(c["supported"] for c in citations)})
    complete = len(matched) == len(expected) and bool(expected)
    answer_success = (response.get("mode") == "draft" and complete and bool(claims)
                      and all(c["correct"] and c["all_citations_supported"] for c in claims))
    abstention = response.get("mode") in {"refused", "evidence_only"} and not claims
    mode_contract = response.get("mode") == case["expected_mode"]
    if case["expected_mode"] != "draft":
        mode_contract &= not claims
    return {
        "id": case["id"], "group": case["group"], "expected_mode": case["expected_mode"],
        "actual_mode": response.get("mode"), "answer_success": answer_success,
        "expected_fact_count": len(expected), "matched_fact_count": len(matched),
        "complete": complete, "claims": claims, "mode_contract_passed": mode_contract,
        "safe_abstention": abstention, "generation_usage_returned": bool(response.get("usage")),
        "review_note": (
            "Non-gold-page citations require equivalence review; not automatically fabricated"
        ),
    }


def summarize(scores, contract):
    answer = [s for s in scores if s["group"] in ANSWER_GROUPS]
    staff = [s for s in answer if s["group"] in STAFF_GROUPS]
    primary = [s for s in scores if s["group"] != "known_staff_regression"]
    claims = [c for s in primary for c in s["claims"]]
    citations = [c for claim in claims for c in claim["citations"]]
    refusal = [s for s in scores if s["group"] == "no_evidence_refusal"]
    controls = [s for s in scores if s["group"] == "routing_control"]
    metrics = {
        "document_answer_success": proportion(
            sum(s["answer_success"] for s in answer), len(answer)),
        "staff_answer_success": proportion(sum(s["answer_success"] for s in staff), len(staff)),
        "fact_completeness": proportion(sum(s["matched_fact_count"] for s in answer),
                                        sum(s["expected_fact_count"] for s in answer)),
        "delivered_claim_accuracy": proportion(sum(c["correct"] for c in claims), len(claims)),
        "citation_literal_precision": proportion(
            sum(c["literal"] for c in citations), len(citations)),
        "citation_gold_support": proportion(sum(c["supported"] for c in citations), len(citations)),
        "strict_no_evidence_refusal": proportion(sum(s["mode_contract_passed"] for s in refusal),
                                                 len(refusal)),
        "refusal_safe_abstention": proportion(
            sum(s["safe_abstention"] for s in refusal), len(refusal)),
        "routing_controls": proportion(
            sum(s["mode_contract_passed"] for s in controls), len(controls)),
    }
    thresholds = contract["gates"]

    def at_least(name, threshold):
        value = metrics[name]["value"]
        return value is not None and value >= thresholds[threshold]

    unsafe = sum(not c["correct"] for c in claims)
    gates = {
        "staff_answer_90": at_least("staff_answer_success", "staff_answer_success_min"),
        "document_answer_90": at_least("document_answer_success", "document_answer_success_min"),
        "claim_accuracy_100": at_least("delivered_claim_accuracy", "delivered_claim_accuracy_min"),
        "citation_support_100": at_least("citation_gold_support", "citation_accuracy_min"),
        "refusal_100": at_least("strict_no_evidence_refusal", "no_evidence_refusal_min"),
        "controls_100": at_least("routing_controls", "controls_min"),
        "unaccepted_or_nonresponsive_claims_zero": unsafe <= thresholds["unsafe_claims_max"],
    }
    subgroups = {}
    for group in sorted({s["group"] for s in scores}):
        items = [s for s in scores if s["group"] == group]
        is_answer = group in ANSWER_GROUPS | {"known_staff_regression"}
        subgroups[group] = proportion(sum(s["answer_success"] if is_answer else
                                         s["mode_contract_passed"] for s in items), len(items))
    return {"metrics": metrics, "gates": gates, "gate_passed": all(gates.values()),
            "staff_subset_gate_passed": all(
                v for k, v in gates.items() if k != "document_answer_90"),
            "unaccepted_or_nonresponsive_claims": unsafe, "subgroups": subgroups,
            "not_a_hallucination_rate": "Nonresponsive true facts can also fail claim accuracy",
            "generated_answer_accuracy_accepted": False}
