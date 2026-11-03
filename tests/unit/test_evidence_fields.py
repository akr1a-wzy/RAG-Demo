from types import SimpleNamespace

from app.retrieval.evaluation import evaluate_answer_fields


def _hit(doc, page, text):
    return SimpleNamespace(document_id=doc, page_start=page, text=text)


def test_numeric_substrings_do_not_count_as_correct_field_values() -> None:
    case = {"expected_keywords": ["300", "14.2"]}
    result = evaluate_answer_fields(case, [_hit("a", 1, "收入3000，同比114.2%")], [])
    assert not any(item["matched"] for item in result)


def test_whitespace_keeps_separate_amount_columns_and_allows_wrapped_units() -> None:
    case = {"expected_keywords": ["294,916", "百万元"]}
    result = evaluate_answer_fields(
        case, [_hit("a", 1, "营业收入 294,916 339,788\n单位 百 万 元")], []
    )
    assert all(item["matched"] for item in result)


def test_field_values_must_come_from_associated_evidence_group() -> None:
    case = {"expected_answer_fields": [
        {"field": "company_a_revenue", "acceptable_values": ["300"],
         "evidence_group_ids": ["a"]}
    ]}
    groups = [{"group_id": "a", "sources": {("a-report", 1)}}]
    hits = [_hit("a-report", 1, "营业收入400"), _hit("b-report", 1, "营业收入300")]
    assert not evaluate_answer_fields(case, hits, groups)[0]["matched"]
    hits[0].text = "营业收入300"
    assert evaluate_answer_fields(case, hits, groups)[0]["matched"]
