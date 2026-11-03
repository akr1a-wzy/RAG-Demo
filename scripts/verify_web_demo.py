"""真实本机HTTP闭环检查；只有显式--live-model-smoke才调用生成接口。"""

import argparse
import json
from pathlib import Path
from time import perf_counter

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--live-model-smoke", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "eval/results/phase7_http_smoke_verified.json")
    args = parser.parse_args()
    results = []
    with httpx.Client(base_url=args.url, timeout=120, trust_env=False) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        sid = client.post("/api/sessions").json()["session_id"]
        cases = [
            ("financial_evidence", "中国神华2025年的营业收入是多少，同比变化多少？", False,
             "evidence_only"),
            ("maotai_evidence", "贵州茅台2025年在职员工数量合计是多少人？", True, "evidence_only"),
            ("financial_followup", "那营业收入是多少？", False, "evidence_only"),
            ("byd_headcount", "比亚迪2025年在职员工数量合计是多少人？",
             not args.live_model_smoke, "draft" if args.live_model_smoke else "evidence_only"),
            ("forecast_refusal", "中国神华管理层给出的2027年股票目标价是多少？", False, "refused"),
            ("market_unsupported", "中国神华当前PE是多少？", False, "unsupported"),
        ]
        for name, question, evidence_only, expected in cases:
            start = perf_counter()
            response = client.post("/api/chat", json={
                "session_id": sid, "question": question, "evidence_only": evidence_only,
            })
            response.raise_for_status()
            data = response.json()
            results.append({"case": name, "expected_mode": expected, "mode": data["mode"],
                            "passed": data["mode"] == expected,
                            "seconds_http": perf_counter() - start, "response": data})
        history_count = len(client.get(f"/api/sessions/{sid}").json()["turns"])
        assert history_count == len(cases)
        reset = client.delete(f"/api/sessions/{sid}/messages")
        assert reset.status_code == 200
        assert client.get(f"/api/sessions/{sid}").json()["turns"] == []
        after_reset = client.post("/api/chat", json={
            "session_id": sid, "question": "那员工数量呢？",
        })
        assert after_reset.json()["mode"] == "clarification"
        pdf = client.get("/documents/601088-2025-annual.pdf", headers={"Range": "bytes=0-99"})
        assert pdf.status_code == 206 and pdf.content.startswith(b"%PDF")
        assert "inline" in pdf.headers["content-disposition"]
        assert client.get("/docs").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
    report = {
        "version": "local-http-demo-v1", "base_url": args.url,
        "passed_count": sum(r["passed"] for r in results), "denominator": len(results),
        "cases": results, "health": health.json(), "history_turns_before_reset": history_count,
        "reset_and_clarification_passed": True, "pdf_range_and_inline_passed": True,
        "live_model_requests_attempted": 1 if args.live_model_smoke else 0,
        "scope": (
            "Local functional smoke on known demo examples, not independent answer accuracy or SLA"
        ),
        "generated_answer_accuracy_accepted": False,
    }
    target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"cases", "health"}},
                     ensure_ascii=False))
    if report["passed_count"] != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
