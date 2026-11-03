import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "evaluate_holdout_once",
    Path(__file__).resolve().parents[2] / "scripts/evaluate_holdout_once.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _report():
    return {"top_k": 5, "summary": {
        "answerable_count": 30, "refusal_not_evaluated": 5,
        "mean_evidence_group_recall_at_5": 0.85, "all_sources_hit_count": 24,
        "entity_correct_count": 30, "company_coverage": {str(i): {} for i in range(20)},
        "companies_with_zero_hits": [], "answer_fields_complete_count": 24,
    }}


def test_holdout_passes_only_with_all_thresholds_met() -> None:
    assert all(_MODULE.holdout_checks(_report()).values())


@pytest.mark.parametrize("key,value", [
    ("all_sources_hit_count", 23), ("answer_fields_complete_count", 23),
    ("companies_with_zero_hits", ["000333"]), ("entity_correct_count", 29),
    ("mean_evidence_group_recall_at_5", 0.8499),
])
def test_recall_alone_cannot_override_other_failed_gates(key, value) -> None:
    report = _report()
    report["summary"][key] = value
    assert not all(_MODULE.holdout_checks(report).values())
