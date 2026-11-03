import pytest

from app.tools.ticker import CODE_TO_CANONICAL_NAME, market_prefix, normalize_ticker


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519"),
        ("SH600519", "600519"),
        ("600519.sh", "600519"),
        ("sz000858", "000858"),
        ("贵州茅台", "600519"),
        ("五粮液", "000858"),
        ("泸州老窖", "000568"),
        ("招行", "600036"),
        ("中石油", "601857"),
        ("中国平安", "601318"),
    ],
)
def test_normalize_ticker(raw: str, expected: str) -> None:
    assert normalize_ticker(raw) == expected


def test_normalize_ticker_rejects_unknown_text() -> None:
    with pytest.raises(ValueError):
        normalize_ticker("未知公司")


@pytest.mark.parametrize("ambiguous", ["平安", "中信"])
def test_ambiguous_short_names_are_rejected(ambiguous: str) -> None:
    with pytest.raises(ValueError):
        normalize_ticker(ambiguous)


def test_all_twenty_canonical_names_normalize() -> None:
    assert len(CODE_TO_CANONICAL_NAME) == 20
    for code, name in CODE_TO_CANONICAL_NAME.items():
        assert normalize_ticker(name) == code


@pytest.mark.parametrize(
    ("code", "prefix"),
    [("600519", "sh"), ("000858", "sz"), ("832000", "bj")],
)
def test_market_prefix(code: str, prefix: str) -> None:
    assert market_prefix(code) == prefix
