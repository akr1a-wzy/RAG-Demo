import pytest

from app.evaluation.answers import citation_score


@pytest.mark.parametrize("first,second", [
    ("40,528,509,770.23", "89,175,178,322.70"),
    ("31,629,416,193.83", "27,984,605,342.06"),
    ("115,636,231,250.05", "115,393,310,976.69"),
    ("423,701,834", "362,012,554"),
])
def test_adjacent_financial_columns_keep_numeric_boundaries(first, second):
    quote = f"营业收入 {first} {second}"
    source = {"content": quote, "document_id": "annual", "physical_page": 7}
    fact = {"row_label": "营业收入", "value": first, "document_id": "annual", "physical_page": 7}
    assert citation_score({"quote": quote}, source, fact, "")['supported']
    fact["value"] = second
    assert citation_score({"quote": quote}, source, fact, "")['supported']
    fact["value"] = first + "9"
    assert not citation_score({"quote": quote}, source, fact, "")['supported']
