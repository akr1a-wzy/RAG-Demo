"""生成前按问题选择目标事实，不能把某一公司有事实当作全题证据齐全。"""

from copy import deepcopy

from app.qa.employees import unsupported_employee_request
from app.qa.facts import decimal_text, metric_in
from app.qa.financial import financial_fact_allowed, supported_financial_request


def select_target_facts(sources: list[dict], resolved: dict) -> tuple[list[dict], dict]:
    query = resolved["query"]
    metric = metric_in(query)
    if unsupported_employee_request(query):
        return [{**s, "facts": []} for s in sources], {
            "status": "unsupported_employee_subset", "metric": metric,
        }
    financial = metric is not None and not metric.startswith("headcount_")
    if financial and not supported_financial_request(resolved):
        return [{**s, "facts": []} for s in sources], {
            "status": "unsupported_financial_request", "metric": metric,
        }
    selected = deepcopy(sources)
    by_company = {code: [] for code in resolved["stock_codes"]}
    for source in selected:
        allowed = []
        for fact in source.get("facts", []):
            if (fact["metric"] != metric or fact["period"] != resolved["report_period"]
                    or fact["stock_code"] not in by_company):
                continue
            if financial and not financial_fact_allowed(fact, query):
                continue
            if (fact.get("entity_scope") == "system") != ("体系" in query):
                continue
            if fact.get("entity_scope") == "filing_company" and "本公司" not in query:
                continue
            allowed.append(fact)
            by_company[fact["stock_code"]].append(fact)
        source["facts"] = allowed
    missing = [code for code, facts in by_company.items() if not facts]
    ambiguous = [code for code, facts in by_company.items() if len({
        (decimal_text(f["value"]), f["unit"], f.get("entity_scope"), f.get("scope_qualifier"))
        for f in facts
    }) > 1]
    status = "ambiguous" if ambiguous else "missing" if missing else "ready"
    return selected, {"status": status, "metric": metric, "missing_companies": missing,
                      "ambiguous_companies": ambiguous}
