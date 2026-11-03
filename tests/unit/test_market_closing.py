from datetime import UTC, datetime

import pytest

from app.market.service import closing_price, plan_market_request


@pytest.mark.parametrize("question,kind", [
    ("招商银行今日收盘价", "closing_price"), ("招行今天的收市价是多少", "closing_price"),
    ("600036最近收盘价", "closing_price"), ("招商银行最新价", "quote"),
    ("招商银行实时报价", "quote"),
    ("招商银行今天收盘多少钱", "closing_price"),
])
def test_close_and_quote_language_does_not_fall_through_to_annual_report(question, kind):
    request = plan_market_request(question, [])
    assert request["kind"] == kind and request["status"] == "supported"
    assert request["resolved"]["stock_codes"] == ["600036"]


@pytest.mark.parametrize("question,status", [
    ("招商银行昨天收盘价", "unsupported"), ("招商银行2025年收盘价", "unsupported"),
    ("招商银行明天收盘价", "refused"), ("招商银行收盘价及PE", "clarification"),
    ("招商银行今日收盘价和营业收入", "clarification"),
    ("招商银行2026-10-26收盘价", "unsupported"),
    ("招商银行上周收盘价", "unsupported"),
])
def test_close_does_not_drop_date_forecast_or_mixed_constraints(question, status):
    assert plan_market_request(question, [])["status"] == status


def data(as_of="2026-10-26T15:50:00+08:00", bar_date="2026-10-26"):
    return {"quote": {"as_of": as_of, "price": "40.25"},
            "bars": [{"date": bar_date, "close": "39.75"}]}


def test_next_day_does_not_present_previous_close_as_today():
    now = datetime(2026, 10, 26, 20, tzinfo=UTC)  # 北京次日04时。
    result = closing_price(data(), {"time_scope": "today"}, now)
    assert not result["available"] and result["value"] is None
    assert result["requested_date"] == "2026-10-27"
    assert result["latest_confirmed"]["date"] == "2026-10-26"


def test_same_day_closed_price_uses_unadjusted_quote_not_qfq_bar():
    now = datetime(2026, 10, 26, 8, tzinfo=UTC)
    result = closing_price(data(), {"time_scope": "today"}, now)
    assert result["available"] and result["value"]["price"] == "40.25"
    assert result["value"]["adjustment"] == "unadjusted"


@pytest.mark.parametrize("as_of,bar_date", [
    ("2026-10-26T10:00:00+08:00", "2026-10-25"),
    ("2026-10-26T15:50:00+08:00", "2026-10-25"),
    ("2026-10-27T15:50:00+08:00", "2026-10-27"),
])
def test_intraday_stale_bars_and_future_business_time_do_not_prove_close(as_of, bar_date):
    result = closing_price(data(as_of, bar_date), {"time_scope": "today"},
                           datetime(2026, 10, 26, 8, tzinfo=UTC))
    assert not result["available"] and result["latest_confirmed"] is None


def test_latest_closed_request_keeps_original_business_date():
    result = closing_price(data(), {"time_scope": "latest_closed"},
                           datetime(2026, 10, 26, 20, tzinfo=UTC))
    assert result["available"] and result["value"]["date"] == "2026-10-26"
