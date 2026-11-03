"""引用/数值出现性及来源元数据检查；不等价于财务行列语义验证。"""

import re
import unicodedata
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.qa.facts import VERSION, bind_claim, metric_in, source_facts
from app.qa.financial import financial_fact_allowed, supported_financial_request


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    quote: str = Field(min_length=4, max_length=3000)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    stock_code: str
    report_period: str
    citations: list[Citation] = Field(min_length=1, max_length=5)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["answer", "insufficient_evidence"]
    claims: list[Claim] = Field(max_length=6)


def canonical(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def number_tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).replace("−", "-")
    text = re.sub(r"(20\d{2})[/.-](\d{1,2})[/.-](\d{1,2})",
                  lambda m: f"{m[1]}年{int(m[2])}月{int(m[3])}日", text)
    return {str(Decimal(s.replace(",", "")).normalize()) for s in re.findall(
        r"(?<![\d.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])", text
    )}


def evidence_sources(hits, review_provider=None) -> list[dict]:
    sources = []
    for number, hit in enumerate(hits, 1):
        content = "\n".join([hit.text, *[
            c.text for c in getattr(hit, "same_page_supporting_chunks", [])
        ]])[:6000]
        source = {
            "source_id": f"S{number}", "document_id": hit.document_id,
            "stock_code": hit.stock_code, "stock_name": hit.stock_name,
            "report_period": hit.report_period, "title": hit.title,
            "document_type": hit.document_type, "section": getattr(hit, "section", ""),
            "physical_page": hit.page_start, "source_url": hit.source_url,
            "content": content,
            "layout_review_required": hit.table_layout_review_required,
        }
        source["facts"] = source_facts(source, review_provider)
        sources.append(source)
    return sources


def validate_draft(payload: dict, sources: list[dict], resolved: dict,
                   review_provider=None, *, require_reviewed_financial=False) -> tuple[Draft, dict]:
    draft = Draft.model_validate(payload)
    errors = []
    bindings = []
    by_id = {s["source_id"]: s for s in sources}
    if draft.status == "answer" and not draft.claims:
        errors.append("empty_answer")
    if draft.status == "insufficient_evidence" and draft.claims:
        errors.append("refusal_contains_claims")
    for i, claim in enumerate(draft.claims, 1):
        quotes = []
        cited = []
        if claim.stock_code not in resolved["stock_codes"]:
            errors.append(f"claim_{i}_unexpected_company")
        if claim.report_period != resolved["report_period"]:
            errors.append(f"claim_{i}_unexpected_report_period")
        for citation in claim.citations:
            source = by_id.get(citation.source_id)
            if source is None:
                errors.append(f"claim_{i}_unknown_source")
                continue
            if (source["stock_code"] != claim.stock_code
                    or source["report_period"] != claim.report_period):
                errors.append(f"claim_{i}_source_metadata_mismatch")
            if source["layout_review_required"] and not (
                review_provider and review_provider.allows_citation(source, citation.quote)
            ):
                errors.append(f"claim_{i}_source_layout_requires_review")
            if (re.search(r"财务公司主要财务指标|子公司主要财务|母公司利润表|母公司资产负债表",
                          source.get("section", ""))
                    and not re.search(r"(?:母公司|子公司|财务公司)(?:的|报表|口径|自身)",
                                      resolved["query"])):
                errors.append(f"claim_{i}_ambiguous_entity_scope")
            if source.get("document_type") == "annual_report":
                years = re.findall(r"(20\d{2})年(?!\d{1,2}月)", claim.text)
                if any(year != claim.report_period for year in years):
                    errors.append(f"claim_{i}_unexpected_year_in_text")
            if canonical(citation.quote) not in canonical(source["content"]):
                errors.append(f"claim_{i}_quote_not_in_source")
            quotes.append(citation.quote)
            cited.append((source, citation.quote))
        quoted = "\n".join(quotes)
        allowed_numbers = number_tokens(quoted) | number_tokens(claim.report_period)
        binding, error = bind_claim(claim.text, claim.report_period, cited, review_provider)
        if error:
            errors.append(f"claim_{i}_{error}")
            units = re.findall(
                r"\d\s*(万亿元|百万元|亿元|万元|千元|元/股|元|万人|人|股|%)", claim.text)
            if any(unit not in canonical(quoted).replace(" ", "") for unit in units):
                errors.append(f"claim_{i}_unsupported_unit")
        else:
            bindings.append({"claim_index": i, **binding})
            allowed_numbers |= number_tokens(binding.get("year_context_quote") or "")
            expected_metric = metric_in(resolved["query"])
            if expected_metric and binding["metric"] != expected_metric:
                errors.append(f"claim_{i}_unexpected_metric")
            is_financial = not binding["metric"].startswith("headcount_")
            if require_reviewed_financial and is_financial and not (
                supported_financial_request(resolved)
                and financial_fact_allowed(binding, resolved["query"])
            ):
                errors.append(f"claim_{i}_financial_fact_requires_review")
        if not number_tokens(claim.text) <= allowed_numbers:
            errors.append(f"claim_{i}_unsupported_number")
    return draft, {
        "checks_passed": not errors, "errors": sorted(set(errors)),
        "checks": ["source_id", "literal_quote", "numeric_presence", "unit_or_count_semantics",
                   "company_and_report_period_metadata", "row_year_value_unit_binding"],
        "binding_version": VERSION, "fact_bindings": bindings,
        "fact_bindings_verified": bool(draft.claims) and not errors,
        "explicit_year_column_bindings_verified": bool(bindings) and not errors and all(
            b["year_basis"] == "explicit_year_columns" for b in bindings
        ),
        # 保留全财务语义未验收标记；简单列绑定不等于合并表头/主体/重述口径全覆盖。
        "financial_row_column_semantics_verified": False,
        "selected_financial_cells_verified": bool(bindings) and not errors and all(
            b.get("version") == "reviewed-financial-cells-v1" for b in bindings
        ),
        "semantic_scope": (
            "Supported single-row facts only; complex/ambiguous tables blocked; "
            "not whole-document acceptance"
        ),
        "generated_answer_accuracy_accepted": False,
    }
