import json

import pytest

from app.retrieval.query_expansion import QueryExpander


def _write_rules(tmp_path, rules):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps({"schema_version": 1, "rules": rules}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _rule():
    return {
        "rule_id": "yoy-v1",
        "canonical": "同比变化",
        "aliases": ["本年比上年增减", "本期比上年同期增减"],
        "scope": {
            "document_types": ["annual_report"],
            "query_terms_any": ["营业收入"],
            "query_terms_excluded": ["期末"],
            "target_sections": ["主要会计数据"],
            "metric_kind": "period_flow",
        },
        "excluded": ["本年末比上年末增减"],
        "provenance": [{"source_type": "dev", "reference": "case", "note": "reviewed"}],
        "review_status": "confirmed",
        "version": 1,
    }


def test_expands_confirmed_rule_without_replacing_original(tmp_path) -> None:
    expander = QueryExpander(_write_rules(tmp_path, [_rule()]))
    result = expander.expand("营业收入同比变化")
    assert result.original_query == "营业收入同比变化"
    assert result.added_terms == ["本期比上年同期增减", "本年比上年增减"]
    assert result.matched_rule_ids == ["yoy-v1"]
    assert result.expanded_query.startswith(result.original_query)


def test_scope_and_exclusions_prevent_unsafe_expansion(tmp_path) -> None:
    expander = QueryExpander(_write_rules(tmp_path, [_rule()]))
    assert expander.expand("资产期末同比变化").added_terms == []
    assert expander.expand("营业收入本年末比上年末增减").added_terms == []
    assert expander.expand("营业收入同比变化", "announcement").added_terms == []


def test_rejects_conflicting_or_duplicate_rules(tmp_path) -> None:
    rule = _rule()
    rule["excluded"] = ["同比变化"]
    with pytest.raises(ValueError):
        QueryExpander(_write_rules(tmp_path, [rule]))
    duplicate = _rule()
    with pytest.raises(ValueError):
        QueryExpander(_write_rules(tmp_path, [duplicate, duplicate]))
