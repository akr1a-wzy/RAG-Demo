from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.config import settings
from app.tools.ticker import market_prefix, normalize_ticker

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="


class ProviderError(RuntimeError):
    """行情提供方返回不可用响应。"""


class RetryableProviderError(ProviderError):
    """可通过有限次数重试恢复的提供方错误。"""


class Quote(BaseModel):
    code: str
    name: str
    price: Decimal | None
    last_close: Decimal | None
    open: Decimal | None
    change_amount: Decimal | None
    change_percent: Decimal | None
    high: Decimal | None
    low: Decimal | None
    amount_wan: Decimal | None
    turnover_percent: Decimal | None
    pe_ttm: Decimal | None
    amplitude_percent: Decimal | None
    market_cap_yi: Decimal | None
    float_market_cap_yi: Decimal | None
    pb: Decimal | None
    limit_up: Decimal | None
    limit_down: Decimal | None
    volume_ratio: Decimal | None
    pe_static: Decimal | None
    as_of: str | None
    fetched_at: datetime
    source: str = "腾讯财经"
    source_url: str


def _decimal(value: str) -> Decimal | None:
    if value in ("", "--", "-", None):
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ProviderError(f"腾讯行情字段不是有效数字: {value!r}") from exc


def parse_tencent_payload(payload: str, fetched_at: datetime | None = None) -> dict[str, Quote]:
    """解析腾讯 GBK 响应解码后的文本，保留数据时间和来源。"""

    fetched_at = fetched_at or datetime.now(UTC)
    quotes: dict[str, Quote] = {}
    for raw_line in payload.split(";"):
        line = raw_line.strip()
        if not line or "=" not in line or '"' not in line:
            continue

        key = line.split("=", 1)[0].rsplit("_", 1)[-1]
        values = line.split('"', 2)[1].split("~")
        if len(values) < 53:
            raise ProviderError(f"腾讯行情字段数量不足: {len(values)}")

        code = normalize_ticker(key[2:])
        quotes[code] = Quote(
            code=code,
            name=values[1],
            price=_decimal(values[3]),
            last_close=_decimal(values[4]),
            open=_decimal(values[5]),
            change_amount=_decimal(values[31]),
            change_percent=_decimal(values[32]),
            high=_decimal(values[33]),
            low=_decimal(values[34]),
            amount_wan=_decimal(values[37]),
            turnover_percent=_decimal(values[38]),
            pe_ttm=_decimal(values[39]),
            amplitude_percent=_decimal(values[43]),
            market_cap_yi=_decimal(values[44]),
            float_market_cap_yi=_decimal(values[45]),
            pb=_decimal(values[46]),
            limit_up=_decimal(values[47]),
            limit_down=_decimal(values[48]),
            volume_ratio=_decimal(values[49]),
            pe_static=_decimal(values[52]),
            as_of=values[30] or None,
            fetched_at=fetched_at,
            source_url=f"{TENCENT_QUOTE_URL}{key}",
        )
    return quotes


class TencentQuoteClient:
    """带明确超时和有限重试的腾讯行情客户端。"""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @retry(
        retry=retry_if_exception_type(
            (httpx.TimeoutException, httpx.TransportError, RetryableProviderError)
        ),
        stop=stop_after_attempt(settings.tencent_max_attempts),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        reraise=True,
    )
    async def fetch(self, codes: list[str]) -> dict[str, Quote]:
        if not codes:
            return {}

        normalized = list(dict.fromkeys(normalize_ticker(code) for code in codes))
        symbols = [f"{market_prefix(code)}{code}" for code in normalized]
        url = TENCENT_QUOTE_URL + ",".join(symbols)
        timeout = httpx.Timeout(
            connect=settings.tencent_connect_timeout_seconds,
            read=settings.tencent_read_timeout_seconds,
            write=settings.tencent_read_timeout_seconds,
            pool=settings.tencent_connect_timeout_seconds,
        )

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout)
        try:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableProviderError(f"腾讯行情暂时不可用: HTTP {response.status_code}")
            response.raise_for_status()
            payload = response.content.decode("gbk")
            result = parse_tencent_payload(payload)
            missing = set(normalized) - set(result)
            if missing:
                raise ProviderError(f"腾讯行情缺少代码: {sorted(missing)}")
            return result
        finally:
            if owns_client:
                await client.aclose()
