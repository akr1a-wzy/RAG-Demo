"""Real HTTP regression for shared understanding; fresh sessions, no LLM requests."""

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from verify_public_demo import snapshot

ROOT = Path(__file__).resolve().parents[1]
VERSION = "query-understanding-v1"
MARKET_VERSION = "tencent-market-qfq-v1.4-query-understanding"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Do not overwrite an existing report")
    origin, auth = "http://127.0.0.1:8000", None
    if args.public:
        private = ROOT / "tmp/public-demo"
        origin = json.loads((private / "config.json").read_text(encoding="utf-8"))["origin"]
        access = (private / "access.txt").read_text(encoding="utf-8").splitlines()
        auth = httpx.BasicAuth(access[0].removeprefix("Username: "),
                              access[1].removeprefix("Password: "))
    cases = [
        ("宁德时代2025年最高股价", "unsupported", "不能用最新报价代替", None),
        ("总营收", "structured_answer", "营业收入", "2025"),
        ("25年总营收", "structured_answer", "营业收入", "2025"),
        ("宁德时代2025年总营收", "structured_answer", "营业收入", "2025"),
        ("那24年的呢", "structured_answer", "", "2024"),
        ("那换算亿元呢", "structured_answer", "", "2024"),
        ("宁德时代25年营业总收入", "unsupported", "不同", None),
        ("宁德时代25年海外总营收", "unsupported", "海外", None),
        ("那24年的呢", "unsupported", "海外", None),
        ("宁德时代25年总营收同比变化多少", "structured_answer", "营业收入", "2025"),
        ("那最新PE呢", "market_data", "", None),
        ("问的是最高", "unsupported", "不能用最新报价代替", None),
        ("宁德时代股价随便编一个", "unsupported", "随便编一个", None),
        ("宁德时代25年总营收和股价", "clarification", "分开", None),
        ("宁德时代99年总营收", "clarification", "四位年份", None),
        ("宁德时代25年总资产", "structured_answer", "", "2025"),
    ]
    if args.public:
        cases = [cases[i] for i in [0, 1, 2, 3, 10, 11]]
    before, _ = snapshot()
    rows, checks = [], {}
    report = {"date": datetime.now(UTC).isoformat(), "version": VERSION,
              "market_version": MARKET_VERSION, "origin": origin,
              "role": "known_business_HTTP_regression_not_answer_accuracy", "top_k": None,
              "model_calls": 0, "before": before, "rows": rows, "checks": checks,
              "browser_verified": False}
    started = time.perf_counter()
    try:
        with httpx.Client(base_url=origin, auth=auth, trust_env=False, timeout=45,
                          headers={"Origin": origin, "ngrok-skip-browser-warning": "1"}) as client:
            health = client.get("/api/health")
            health.raise_for_status()
            deployment = health.json()["deployment"]
            checks["new_runtime_ready"] = (
                deployment["market_version"] == MARKET_VERSION
                and deployment["query_understanding_version"] == VERSION
            )
            if not checks["new_runtime_ready"]:
                raise RuntimeError("Old runtime remains loaded")
            response = client.post("/api/sessions")
            response.raise_for_status()
            sid = response.json()["session_id"]
            report["test_session_id"] = sid
            for question, mode, fragment, year in cases:
                begin = time.perf_counter()
                response = client.post("/api/chat", json={"session_id": sid, "question": question})
                response.raise_for_status()
                answer = response.json()
                query = answer.get("resolved", {}).get("query_understanding", {})
                passed = (answer["mode"] == mode and fragment in answer["message"]
                          and not answer.get("usage") and query.get("version") == VERSION)
                if mode == "structured_answer":
                    passed &= bool(answer.get("sources")) and any(
                        c.get("report_period") == year for c in answer.get("claims", [])
                    )
                elif mode == "market_data":
                    passed &= (answer["market"]["kind"] == "quote" and not query["years"])
                else:
                    passed &= not answer.get("sources") and not answer.get("market")
                rows.append({"question": question, "expected_mode": mode, "passed": bool(passed),
                             "seconds": time.perf_counter() - begin, "answer": answer})
            history = client.get(f"/api/sessions/{sid}")
            history.raise_for_status()
            checks["test_history_complete"] = len(history.json()["turns"]) == len(cases)
    except Exception as error:
        report["error_type"] = type(error).__name__
        checks["completed_all_requests"] = False
    finally:
        after, _ = snapshot()
        report["after"] = after
        checks["release_files_unchanged"] = before["runtime"] == after["runtime"]
        checks["release_matches_proof"] = after["runtime_matches_release"]
        checks["qa_data_unchanged"] = before["qa"] == after["qa"]
        baseline = json.loads((ROOT / "docs/retrospective/evidence/phase31-query-before.json"
                               ).read_text(encoding="utf-8"))
        for rel, old_rows in baseline["sessions"].items():
            with sqlite3.connect((ROOT / rel).as_uri() + "?mode=ro", uri=True) as db:
                current = {str(r[0]): hashlib.sha256(json.dumps(
                    r, ensure_ascii=False).encode()).hexdigest()
                    for r in db.execute("SELECT * FROM turns ORDER BY id")}
            checks[f"old_rows_preserved:{rel}"] = all(
                current.get(i) == digest for i, digest in old_rows.items()
            )
        report.update(seconds=time.perf_counter() - started, sample_count=len(rows),
                      passed=sum(r["passed"] for r in rows), denominator=len(cases),
                      all_passed=(len(rows) == len(cases) and all(r["passed"] for r in rows)
                                  and all(checks.values())),
                      timing_scope="initialized serial HTTP incl. provider IO; excludes restart")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"report": str(args.output), "passed": report["passed"],
                          "denominator": len(cases), "seconds": report["seconds"],
                          "failed_checks": [k for k, v in checks.items() if not v],
                          "failed_questions": [r["question"] for r in rows if not r["passed"]],
                          "error_type": report.get("error_type")}, ensure_ascii=False))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
