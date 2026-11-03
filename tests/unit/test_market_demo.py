import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.market.service import (
    MarketError,
    MarketService,
    SnapshotStore,
    parse_qfq,
    plan_market_request,
    window,
)
from app.tools.tencent import ProviderError, Quote

NOW = datetime(2026, 10, 26, 8, tzinfo=UTC)


def rows(count=251):
    start = NOW.date() - timedelta(days=count)
    return [[(start + timedelta(days=i)).isoformat(), str(100 + i), str(101 + i),
             str(102 + i), str(99 + i), "1000"] for i in range(count)]


def payload(raw=None, key="qfqday"):
    return {"code": 0, "data": {"sh600519": {key: raw if raw is not None else rows()}}}


@pytest.mark.parametrize("question,kind,days", [
    ("茅台最新股价和PE、PB、市值", "quote", 20),
    ("茅台PE是多少", "quote", 20),
    ("茅台最近20个交易日日K", "kline", 20),
    ("茅台250个交易日涨跌幅及K线", "kline", 250),
    ("茅台最近1个交易日K线", "kline", 1),
])
def test_market_request_fields(question, kind, days):
    request = plan_market_request(question, [])
    assert request["status"] == "supported"
    assert request["kind"] == kind and request["days"] == days


@pytest.mark.parametrize("question,status", [
    ("茅台251个交易日日K", "unsupported"),
    ("茅台0个交易日日K", "unsupported"),
    ("茅台2025年股价是多少", "unsupported"),
    ("茅台不复权日K", "unsupported"),
    ("茅台20日与30日K线", "clarification"),
    ("茅台最近20天日K", "clarification"),
    ("茅台明天股价会涨吗", "refused"),
    ("茅台合理估值多少", "clarification"),
    ("茅台营业收入和当前市值", "clarification"),
    ("茅台和宁德的日K", "clarification"),
    ("999999最新股价", "unsupported"),
    ("股价是多少", "clarification"),
])
def test_unsupported_constraints_are_preserved(question, status):
    assert plan_market_request(question, [])["status"] == status


def test_market_followup_does_not_use_inherited_report_year_as_quote_asof():
    r = plan_market_request("那最新PE呢", [{"role": "user", "content": "茅台2025年员工人数"}])
    assert r["status"] == "supported" and r["resolved"]["stock_codes"] == ["600519"]
    assert plan_market_request("茅台2025年员工人数", []) is None


@pytest.mark.parametrize("kind", ["day", "hfqday"])
def test_unadjusted_or_backward_adjusted_payload_does_not_masquerade_as_qfq(kind):
    with pytest.raises(MarketError, match="qfqday"):
        parse_qfq(payload(key=kind), "600519", NOW)


@pytest.mark.parametrize("change", ["duplicate", "future", "nan", "ohlc", "negative_volume"])
def test_bad_bars_are_rejected(change):
    raw = rows()
    if change == "duplicate":
        raw[-1][0] = raw[-2][0]
    elif change == "future":
        raw[-1][0] = "2099-01-01"
    elif change == "nan":
        raw[-1][2] = "NaN"
    elif change == "ohlc":
        raw[-1][3] = "1"
    else:
        raw[-1][5] = "-1"
    with pytest.raises(MarketError):
        parse_qfq(payload(raw), "600519", NOW)


def test_window_uses_extra_baseline_for_20_and_250_days():
    bars = parse_qfq(payload(), "600519", NOW)
    for days in [20, 250]:
        w = window(bars, days)
        expected = (Decimal(bars[-1]["close"]) / Decimal(bars[-days - 1]["close"]) - 1) * 100
        assert len(w["bars"]) == days and w["internal_bar_count"] == days + 1
        assert Decimal(w["return_percent"]) == expected.quantize(Decimal("0.0001"))
    with pytest.raises(MarketError):
        window(bars[:20], 20)


def test_incomplete_today_bar_is_excluded_before_market_close():
    raw = rows(2)
    raw[-1][0] = NOW.date().isoformat()
    assert len(parse_qfq(payload(raw), "600519", NOW - timedelta(hours=2))) == 1
    assert len(parse_qfq(payload(raw), "600519", NOW)) == 2


def quote():
    data = {key: None for key in Quote.model_fields}
    data.update(code="600519", name="贵州茅台", price="1349.98", as_of="20261026155000",
                fetched_at=NOW, source="腾讯财经", source_url="https://qt.gtimg.cn/q=sh600519")
    return Quote(**data)


class Quotes:
    def __init__(self):
        self.fail, self.calls = False, 0

    async def fetch(self, codes):
        self.calls += 1
        if self.fail:
            raise ProviderError("provider response must not be exposed")
        return {"600519": quote()}


def test_live_failure_never_auto_replays_and_explicit_replay_is_network_free(tmp_path):
    quotes = Quotes()
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload())
    )) as client:
        market = MarketService(tmp_path, quote_client=quotes, http_client=client, clock=lambda: NOW)
        request = plan_market_request("茅台最近20个交易日日K", [])
        live = market.ask(request)
        assert live["mode"] == "market_data" and live["market"]["data_mode"] == "live"
        quotes.fail = True
        failed = market.ask(request)
        assert failed["mode"] == "market_error" and "datasets" not in failed["market"]
        assert "provider response" not in json.dumps(failed)
        assert failed["market"]["can_replay"]
        before = quotes.calls
        replay = market.ask(request, replay=True)
        assert replay["mode"] == "market_data" and replay["market"]["data_mode"] == "replay"
        assert quotes.calls == before
        assert replay["market"]["datasets"][0]["fetched_at"] == live["market"]["datasets"][0][
            "fetched_at"]


def test_missing_corrupt_and_mismatched_snapshots_are_not_used(tmp_path):
    market = MarketService(tmp_path, clock=lambda: NOW)
    request = plan_market_request("茅台股价", [])
    assert market.ask(request, replay=True)["mode"] == "market_error"
    store = SnapshotStore(tmp_path)
    store.save("600519", {"quote": {"code": "600519"}, "bars": []})
    path = tmp_path / "600519.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["payload"]["quote"]["code"] = "300750"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MarketError):
        store.load("600519")


def test_intraday_250_window_has_251_closed_bars_plus_optional_open_bar(tmp_path):
    now = NOW - timedelta(hours=6)
    raw = rows(252)
    raw[-1][0] = now.date().isoformat()

    class MorningQuotes:
        async def fetch(self, codes):
            return {"600519": quote().model_copy(update={"as_of": "20261026095000"})}

    def handler(request):
        assert ",252,qfq" in request.url.params["param"]
        return httpx.Response(200, json=payload(raw))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        market = MarketService(tmp_path, quote_client=MorningQuotes(), http_client=client,
                               clock=lambda: now)
        answer = market.ask(plan_market_request("茅台250个交易日日K", []))
    assert answer["mode"] == "market_data"
    chart = answer["market"]["datasets"][0]["chart"]
    assert len(chart["bars"]) == 250 and chart["internal_bar_count"] == 251
    assert chart["bars"][-1]["date"] < now.date().isoformat()
