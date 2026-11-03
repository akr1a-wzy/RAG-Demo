from datetime import UTC, datetime
from decimal import Decimal

from app.tools.tencent import parse_tencent_payload


def _sample_payload() -> str:
    values = [""] * 53
    values[1] = "贵州茅台"
    values[2] = "600519"
    values[3] = "1500.00"
    values[4] = "1490.00"
    values[5] = "1495.00"
    values[30] = "20260915153000"
    values[31] = "10.00"
    values[32] = "0.67"
    values[33] = "1510.00"
    values[34] = "1480.00"
    values[37] = "123456.78"
    values[38] = "0.30"
    values[39] = "22.50"
    values[43] = "2.01"
    values[44] = "18800.00"
    values[45] = "18800.00"
    values[46] = "8.20"
    values[47] = "1639.00"
    values[48] = "1341.00"
    values[49] = "1.10"
    values[52] = "23.00"
    return f'v_sh600519="{"~".join(values)}";'


def test_parse_tencent_payload_uses_correct_field_indexes() -> None:
    fetched_at = datetime(2026, 9, 15, tzinfo=UTC)
    quote = parse_tencent_payload(_sample_payload(), fetched_at)["600519"]

    assert quote.name == "贵州茅台"
    assert quote.price == Decimal("1500.00")
    assert quote.pe_ttm == Decimal("22.50")
    assert quote.amplitude_percent == Decimal("2.01")
    assert quote.pb == Decimal("8.20")
    assert quote.market_cap_yi == Decimal("18800.00")
    assert quote.as_of == "20260915153000"
    assert quote.fetched_at == fetched_at
    assert quote.source == "腾讯财经"
