import argparse
import asyncio

from app.tools.tencent import TencentQuoteClient


async def main(codes: list[str]) -> None:
    quotes = await TencentQuoteClient().fetch(codes)
    for quote in quotes.values():
        print(
            f"{quote.name}({quote.code}) 价格={quote.price} "
            f"涨跌幅={quote.change_percent}% PE={quote.pe_ttm} PB={quote.pb} "
            f"时间={quote.as_of} 来源={quote.source}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="检查腾讯财经行情适配器")
    parser.add_argument("codes", nargs="+", help="股票代码或支持的公司名")
    args = parser.parse_args()
    asyncio.run(main(args.codes))
