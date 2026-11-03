"""逐家验证真实行情、20/250日前复权窗口与显式回放；不调用LLM。"""

import argparse
import json
from pathlib import Path
from time import perf_counter

import httpx

from app.market.service import RUNTIME_VERSION, MarketService, plan_market_request
from app.tools.ticker import CODE_TO_CANONICAL_NAME

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("不覆盖已有实测报告")
    service, records, started = MarketService(ROOT), [], perf_counter()
    for code, name in CODE_TO_CANONICAL_NAME.items():
        request = plan_market_request(name + "最新股价和PE、PB、市值", [])
        begin = perf_counter()
        live = service.ask(request)
        replay = service.ask(plan_market_request(name + "最近250个交易日日K", []), replay=True)
        passed = (live["mode"] == replay["mode"] == "market_data"
                  and live["market"]["data_mode"] == "live"
                  and replay["market"]["data_mode"] == "replay")
        if passed:
            d = replay["market"]["datasets"][0]
            passed &= len(d["chart"]["bars"]) == 250 and d["chart"]["internal_bar_count"] == 251
            passed &= d["quote"]["code"] == code and d["adjustment"] == "qfq"
            passed &= len(live["market"]["datasets"][0]["chart"]["bars"]) == 20
        records.append({"stock_code": code, "live": live, "replay_250": replay,
                        "passed": bool(passed), "seconds": perf_counter() - begin})
        print(json.dumps({"completed": len(records), "stock_code": code, "passed": passed}),
              flush=True)
    # 预定本机故障注入，不能当提供方实际事故或线上稳定性指标。
    class FailingQuotes:
        calls = 0

        async def fetch(self, codes):
            self.calls += 1
            raise httpx.ConnectError("planned local fault injection")

    failing = FailingQuotes()
    service.quotes = failing
    request = plan_market_request("茅台最新股价", [])
    failure = service.ask(request)
    before = failing.calls
    replay = service.ask(request, replay=True)
    checks = {"explicit_failure_no_data": failure["mode"] == "market_error"
              and not failure["market"].get("datasets"),
              "no_automatic_replay": failure["market"]["data_mode"] == "live",
              "manual_replay": replay["mode"] == "market_data"
              and replay["market"]["data_mode"] == "replay",
              "replay_no_network": failing.calls == before}
    report = {"date": "2026-10-26", "version": RUNTIME_VERSION, "records": records,
              "sample_count": len(records), "passed": sum(r["passed"] for r in records),
              "denominator": len(records), "fault_injection_checks": checks,
              "fault_injection_response": failure, "model_calls": 0,
              "seconds": perf_counter() - started,
              "timing_scope": "Local serial live market fetches, snapshot IO and manual replay",
              "gate_passed": all(r["passed"] for r in records) and all(checks.values())}
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ["passed", "denominator", "gate_passed", "seconds"]}))
    if not report["gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
