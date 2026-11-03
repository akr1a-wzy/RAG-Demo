import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.qa.evidence import validate_draft
from app.qa.facts import source_facts
from app.qa.reviews import ReviewedEmployeeFacts

ROOT = Path(__file__).resolve().parents[2]


def oil_source():
    path = ROOT / "eval/results/answer_acceptance_v1_raw.jsonl"
    records = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
    return next(r["response"]["sources"][0] for r in records if r["id"] == "answer-v1-staff-601857")


def payload(quote="合计 367,173 100.0", value="367,173"):
    return {"status": "answer", "claims": [{
        "text": "中国石油2025年在职员工数量合计" + value + "人",
        "stock_code": "601857", "report_period": "2025",
        "citations": [{"source_id": "S1", "quote": quote}],
    }]}


RESOLVED = {"query": "中国石油2025年在职员工数量", "stock_codes": ["601857"],
            "report_period": "2025"}


def test_only_reviewed_cell_can_pass_while_whole_page_flag_stays_set():
    source = oil_source()
    assert source["layout_review_required"] and not source_facts(source)
    provider = ReviewedEmployeeFacts(ROOT)
    facts = source_facts(source, provider)
    assert len(facts) == 1 and facts[0]["value"] == "367,173"
    _, before = validate_draft(payload(), [source], RESOLVED)
    _, after = validate_draft(payload(), [source], RESOLVED, provider)
    assert not before["checks_passed"] and after["checks_passed"]
    assert source["layout_review_required"]
    assert not after["financial_row_column_semantics_verified"]


def test_other_row_and_percentage_are_not_covered_by_the_review():
    provider = ReviewedEmployeeFacts(ROOT)
    source = oil_source()
    assert not provider.allows_citation(source, "经营管理人员 107,478 29.3")
    _, checks = validate_draft(payload("经营管理人员 107,478 29.3", "107,478"),
                               [source], RESOLVED, provider)
    assert not checks["checks_passed"]


@pytest.mark.parametrize("mutation", [
    "document", "page", "image_hash", "cell", "period", "pdf_hash",
])
def test_review_requires_all_provenance_and_geometry_proofs(mutation):
    provider = ReviewedEmployeeFacts(ROOT)
    provider.reviews = deepcopy(provider.reviews)
    source = oil_source()
    if mutation == "document":
        source["document_id"] = "different-document"
    elif mutation == "page":
        source["physical_page"] = 89
    elif mutation == "image_hash":
        provider.reviews[0]["evidence_image_sha256"] = "0" * 64
    elif mutation == "cell":
        provider.reviews[0]["value_bbox"] = [380, 209, 510, 223]
    elif mutation == "period":
        provider.reviews[0]["period"] = "2024"
    else:
        provider.reviews[0]["pdf_sha256"] = "0" * 64
    assert not provider.facts_for(source)


def test_source_cannot_forge_a_reviewed_fact_without_trusted_provider():
    source = oil_source()
    source["facts"] = [{"metric": "headcount_total", "value": "367,173", "review_id": "forged"}]
    assert not source_facts(source)


def test_reviewed_citic_narrative_does_not_unblock_garbled_chart():
    diagnostic = json.loads((ROOT / "eval/results/phase12_citic_query_diagnostic.json").read_text(
        encoding="utf-8"))
    source = diagnostic["cases"][0]["response"]["sources"][0]
    provider = ReviewedEmployeeFacts(ROOT)
    assert source["layout_review_required"] and not source_facts(source)
    facts = source_facts(source, provider)
    assert len(facts) == 1 and facts[0]["value"] == "26,823"
    assert provider.allows_citation(source, facts[0]["row_quote"])
    assert not provider.allows_citation(source, "母公司在职员工的数量 15,644")
