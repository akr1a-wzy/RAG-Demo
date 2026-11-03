import asyncio
import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

import httpx

from app.qa.resolution import resolve_document_query
from app.tools.tencent import ProviderError, TencentQuoteClient
from app.tools.ticker import CODE_TO_CANONICAL_NAME, market_prefix

VERSION = "tencent-market-qfq-v1"
RUNTIME_VERSION = "tencent-market-qfq-v1.3-constraint-routing"
SHANGHAI = timezone(timedelta(hours=8))
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
FORECAST = re.compile(
    r"预测|会不会涨|能涨到|翻倍|买入|卖出|目标价|一定会|必涨|推荐买|未来|明天|明年|下周|下个?月|会涨|会跌"
)
CLOSE = re.compile(r"收(?:盘|市)(?:价|多少钱|多少|是|报|为|$)")
MARKET = re.compile(
    r"现价|股价|行情|价格|报价|最新价|收盘价|收市价|市盈率|市净率|市值|估值"
    r"|(?<![A-Za-z])(?:PE|PB)(?![A-Za-z])"
    r"|日K|K线|交易日|\d+日(?:收益|涨跌)", re.I,
)
YEAR = re.compile(r"(?<!\d)(20\d{2}|\d{2})\s*年")
EXTREME = re.compile(r"最高|最低|最大|最小|峰值|高点|低点")
DOCUMENT = re.compile(
    r"年报|财务|营收|营业收入|员工|人数|人员|利润|资产|负债|现金|分红|股利|派息|公告|薪酬"
)
TIME_SCOPE = re.compile(
    r"最新|实时|当前|现价|最近|今日|今天|今年|去年|前年|昨日|昨天|前天|上周|上月"
)
CORRECTION = re.compile(
    r"(?:那|那么|我问的是|问的是|我要的是|要的是|改成|换成|不对[，,\s]*)?\s*"
    r"(?:(?:20\d{2}|\d{2})\s*年(?:的)?|今天|今日|今年|去年|前年)?"
    r"(?:最高|最低|最大|最小|峰值|高点|低点)?(?:价|价格|股价|收盘价)?"
    r"(?:的|呢|是多少|多少|多少钱|啊|呀|吗|[？?。！!\s])*"
)


def normalize_market_years(question: str) -> str:
    # 两位年度仅在行情路由内按本世纪展开，不改变文档报告期解析。
    return YEAR.sub(lambda m: ("20" + m[1] if len(m[1]) == 2 else m[1]) + "年", question)


def effective_market_question(question: str, history: list[dict]) -> str | None:
    """仅延续相邻行情话题的受限纠正表达，文档或未知话题切断继承。"""
    context = None
    for text in [m.get("content", "") for m in history if m.get("role") == "user"] + [question]:
        text = normalize_market_years(text.strip())
        explicit = bool(MARKET.search(text) or CLOSE.search(text))
        correction = bool(CORRECTION.fullmatch(text) and (
            EXTREME.search(text) or YEAR.search(text) or TIME_SCOPE.search(text)
        ))
        if explicit:
            inherit = context is not None and not TIME_SCOPE.search(text) and (
                EXTREME.search(text)
                or (re.match(r"^(?:那|那么|它的|我问的是|问的是)", text)
                    and (YEAR.search(context) or EXTREME.search(context)))
            )
        elif correction and context is not None:
            inherit = True
        else:
            context = None
            continue
        if DOCUMENT.search(text) and not explicit:
            context = None
            continue
        if inherit:
            # 明示的新年度/日期替换上一次条件；最新行情不从文档继承日期。
            if YEAR.search(text) or TIME_SCOPE.search(text):
                context = TIME_SCOPE.sub("", YEAR.sub("", context))
            if EXTREME.search(text):
                context = EXTREME.sub("", context)
            context = f"{context} {text}"
        else:
            context = text
    return context


class MarketError(ValueError):
    pass


def plan_market_request(question: str, history: list[dict]) -> dict | None:
    effective = effective_market_question(question, history)
    if effective is None:
        if not history and EXTREME.fullmatch(question.strip(" ？?。！!")):
            return {"kind": "quote", "days": 20, "status": "clarification",
                    "resolved": resolve_document_query(question, history),
                    "message": "请明确公司、时间范围，以及最高或最低指股价还是其他指标。"}
        return None
    resolved = resolve_document_query(normalize_market_years(question), history)
    question = effective
    years = list(dict.fromkeys(YEAR.findall(question)))
    if years:
        resolved["report_period"] = years[-1]
    resolved["market_intent"] = {
        "question": question, "years": years,
        "operation": EXTREME.findall(question)[-1] if EXTREME.search(question) else None,
    }
    kind = "kline" if re.search(r"日K|K线|交易日|\d+日(?:收益|涨跌)", question, re.I) else "quote"
    request = {"kind": kind, "days": 20, "resolved": resolved, "status": "supported"}
    if CLOSE.search(question):
        request.update(kind="closing_price", time_scope=(
            "today" if re.search(r"今日|今天|当日", question) else "latest_closed"))
        if re.search(r"日K|K线|交易日|PE|PB|市盈率|市净率|市值", question, re.I):
            return {**request, "status": "clarification",
                    "message": "请将收盘价与日K或估值字段分开查询。"}
    if FORECAST.search(question):
        return {**request, "status": "refused", "message": "现有数据不能支持股价预测或买卖建议。"}
    if not resolved["stock_codes"]:
        return {**request, "status": "clarification", "message": "请指定公司或股票代码。"}
    if not set(resolved["stock_codes"]) <= set(CODE_TO_CANONICAL_NAME):
        return {**request, "status": "unsupported", "message": "行情演示仅覆盖登记的20家公司。"}
    if DOCUMENT.search(question):
        return {**request, "status": "clarification", "message": "请将文档问题和行情问题分开提问。"}
    if EXTREME.search(question):
        scope = "、".join(years) + "年" if years else "指定区间"
        return {**request, "status": "unsupported", "message": (
            f"当前不支持查询{scope}的最高或最低行情值，不能用最新报价代替。"
            "支持最新股价、PE、PB、市值及最近交易日的前复权日K。")}
    if re.search(r"20\d{2}年|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}月\d{1,2}日"
                 r"|昨天|昨日|前天|上周|上月|去年|后复权|不复权|分钟|周K|月K|美元|港元",
                 question, re.I):
        return {**request, "status": "unsupported",
                "message": "支持最新行情及最近交易日的前复权日K；不支持历史时点或其他复权口径。"}
    if re.search(r"合理|低估|高估|便宜|值得|\d+天", question):
        return {**request, "status": "clarification",
                "message": "当前支持源数据估值及按交易日的窗口，请明确字段或交易日数。"}
    counts = re.findall(r"(\d+)(?:个)?(?:交易)?日", question)
    if len(set(counts)) > 1:
        return {**request, "status": "clarification", "message": "请一次指定一个交易日窗口。"}
    if counts:
        request["days"] = int(counts[0])
    if not 1 <= request["days"] <= 250:
        return {**request, "status": "unsupported", "message": "日K窗口须为1至250个交易日。"}
    if kind == "kline" and len(resolved["stock_codes"]) != 1:
        return {**request, "status": "clarification", "message": "请一次查询一家公司的日K。"}
    if len(resolved["stock_codes"]) > 2:
        return {**request, "status": "unsupported", "message": "行情比较一次最多查询两家公司。"}
    return request


def closing_price(payload: dict, request: dict, now: datetime) -> dict:
    """只把已收盘业务时间且与完整日线日期一致的最新报价当不复权收盘价。"""
    quote = payload["quote"]
    as_of = datetime.fromisoformat(quote["as_of"])
    if as_of.tzinfo is None:
        raise MarketError("报价时间缺少时区")
    as_of, local = as_of.astimezone(SHANGHAI), now.astimezone(SHANGHAI)
    latest = None
    if (as_of <= local and as_of.time() >= time(15) and payload["bars"]
            and payload["bars"][-1]["date"] == as_of.date().isoformat()
            and number(quote["price"]) > 0):
        latest = {"date": as_of.date().isoformat(), "price": str(quote["price"]),
                  "unit": "元", "adjustment": "unadjusted", "source_field": "quote.price",
                  "business_time": quote["as_of"]}
    requested_date = local.date().isoformat() if request["time_scope"] == "today" else None
    value = (latest if latest and (requested_date is None or latest["date"] == requested_date)
             else None)
    return {"available": value is not None, "value": value, "requested_date": requested_date,
            "latest_confirmed": latest, "message": (
                "已核对收盘业务时间和完整日线日期，列示不复权收盘价。" if value else
                "今日收盘价暂无同日可核验数据；不能将盘中价或上一交易日价格当作今日收盘价。"
                if requested_date else
                "当前报价尚不能核验为已收盘价格；可查询最新股价或前复权日K。")}


def number(value) -> Decimal:
    try:
        n = Decimal(str(value))
    except InvalidOperation as error:
        raise MarketError("提供方数字格式无效") from error
    if not n.is_finite():
        raise MarketError("提供方含非有限数字")
    return n


def parse_qfq(payload: dict, code: str, now: datetime) -> list[dict]:
    symbol = market_prefix(code) + code
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise MarketError("日线提供方返回失败状态")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise MarketError("日线缺少指定公司数据")
    series = data[symbol]
    # 不接受提供方悄悄返回day；前复权必须由qfqday明确标记。
    raw = series.get("qfqday")
    if not isinstance(raw, list) or not raw:
        raise MarketError("未返回明确的前复权qfqday数据")
    bars, last = [], None
    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            raise MarketError("日线字段缺失")
        day = date.fromisoformat(row[0])
        if day > now.astimezone(SHANGHAI).date() or (last and day <= last):
            raise MarketError("日线日期重复、倒序或超前")
        o, c, h, low = [number(x) for x in row[1:5]]
        if min(o, c, h, low) <= 0 or low > min(o, c) or h < max(o, c):
            raise MarketError("OHLC价格关系不合法")
        if number(row[5]) < 0:
            raise MarketError("成交量不合法")
        bars.append({"date": row[0], "open": str(o), "close": str(c),
                     "high": str(h), "low": str(low)})
        last = day
    local = now.astimezone(SHANGHAI)
    if bars[-1]["date"] == local.date().isoformat() and local.time() < time(15):
        bars.pop()  # 当日尚未收盘，不能冒充最近收盘价计算收益。
    return bars


def window(bars: list[dict], days: int) -> dict:
    if not 1 <= days <= 250 or len(bars) < days + 1:
        raise MarketError("收盘日线不足以覆盖窗口和前一基准收盘价")
    selected = bars[-days:]
    base = bars[-days - 1]
    change = (number(selected[-1]["close"]) / number(base["close"]) - 1) * 100
    return {"days": days, "bars": selected, "baseline": base,
            "return_percent": str(change.quantize(Decimal("0.0001"))),
            "return_formula": "(last_close / close_N_trading_days_ago - 1) * 100",
            "internal_bar_count": days + 1, "adjustment": "qfq",
            "adjustment_verification": "provider_qfqday_key", "price_unit": "元"}


class SnapshotStore:
    def __init__(self, directory: Path):
        self.directory = directory

    @staticmethod
    def encode(payload):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save(self, code: str, payload: dict):
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(self.encode(payload).encode()).hexdigest()
        envelope = {"version": VERSION, "sha256": digest, "payload": payload}
        temp = self.directory / (uuid4().hex + ".tmp")
        temp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.directory / (code + ".json"))

    def load(self, code: str) -> dict:
        payload = json.loads((self.directory / (code + ".json")).read_text(encoding="utf-8"))
        if (payload["version"] != VERSION or hashlib.sha256(self.encode(
                payload["payload"]).encode()).hexdigest() != payload["sha256"]
                or payload["payload"]["quote"]["code"] != code):
            raise MarketError("回放快照校验失败")
        return payload["payload"]


class MarketService:
    def __init__(self, root: Path, *, quote_client=None, http_client=None, clock=None):
        self.store = SnapshotStore(root / "data/market_snapshots")
        self.quotes = quote_client or TencentQuoteClient()
        self.http_client = http_client
        self.clock = clock or (lambda: datetime.now(UTC))

    def fetch(self, code: str) -> dict:
        quote = asyncio.run(self.quotes.fetch([code]))[code]
        now = self.clock()
        as_of = datetime.strptime(quote.as_of or "", "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        if as_of > now + timedelta(minutes=5) or quote.price is None or number(quote.price) <= 0:
            raise MarketError("报价缺少有效价格或业务时间")
        q = quote.model_dump(mode="json")
        q["as_of"] = as_of.isoformat()
        for key in ["pe_ttm", "pb", "market_cap_yi"]:
            if q[key] is not None:
                number(q[key])
        symbol = market_prefix(code) + code
        client = self.http_client or httpx.Client(timeout=12, trust_env=False)
        try:
            # 251根已收盘数据之外，允许提供方多带一根盘中未收盘K线。
            response = client.get(KLINE_URL, params={"param": f"{symbol},day,,,252,qfq"},
                                  headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            bars = parse_qfq(response.json(), code, now)[-251:]
        finally:
            if self.http_client is None:
                client.close()
        return {"quote": q, "bars": bars, "fetched_at": now.isoformat(),
                "kline_source": "腾讯财经前复权日线", "kline_source_url": KLINE_URL,
                "adjustment": "qfq", "version": VERSION}

    def ask(self, request: dict, *, replay=False) -> dict:
        resolved = request["resolved"]
        common = {"resolved": resolved, "generated_answer_accuracy_accepted": False}
        if request["status"] != "supported":
            return {**common, "mode": request["status"], "message": request["message"]}
        datasets, codes = [], resolved["stock_codes"]
        try:
            for code in codes:
                payload = self.store.load(code) if replay else self.fetch(code)
                if payload.get("adjustment") != "qfq":
                    raise MarketError("复权口径不匹配")
                chart = window(payload["bars"], request["days"])
                if not replay:
                    self.store.save(code, payload)
                datasets.append({**payload, "bars": chart["bars"], "chart": chart})
                if request["kind"] == "closing_price":
                    datasets[-1]["closing_price"] = closing_price(payload, request, self.clock())
        except (httpx.HTTPError, ProviderError, OSError, ValueError,
                KeyError, TypeError, IndexError) as error:
            return {**common, "mode": "market_error", "message": (
                "回放快照不可用或校验失败。" if replay else
                "行情源暂不可用，本次未返回行情。可手动切换已校验快照回放。"),
                "market": {"kind": request["kind"], "data_mode": "replay" if replay else "live",
                           "can_replay": all((self.store.directory / (c + ".json")).is_file()
                                             for c in codes), "error_type": type(error).__name__}}
        return {**common, "mode": "market_data", "message": (
            "快照回放，非实时行情。" if replay else "已获取行情，可能延迟，请查看业务时间。"),
            "market": {"version": RUNTIME_VERSION, "kind": request["kind"],
                       "data_mode": "replay" if replay else "live", "datasets": datasets,
                       "served_at": self.clock().isoformat(), "can_replay": True}}
