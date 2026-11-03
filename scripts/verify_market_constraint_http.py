"""Verify fixed routing over real HTTP in fresh sessions; no generation requests."""

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
VERSION = "tencent-market-qfq-v1.3-constraint-routing"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Never overwrite a previous report")
    origin, auth = "http://127.0.0.1:8000", None
    if args.public:
        private = ROOT / "tmp/public-demo"
        origin = json.loads((private / "config.json").read_text(encoding="utf-8"))["origin"]
        access = (private / "access.txt").read_text(encoding="utf-8").splitlines()
        auth = httpx.BasicAuth(access[0].removeprefix("Username: "),
                              access[1].removeprefix("Password: "))
    before, _ = snapshot()
    checks, rows = {}, []
    report = {"date": datetime.now(UTC).isoformat(), "version": VERSION, "origin": origin,
              "role": "real_HTTP_known_business_regression_not_answer_accuracy",
              "top_k": None, "model_calls": 0, "rows": rows, "checks": checks,
              "before": before, "browser_verified": False}
    cases = [
        ("宁德时代2025年财务概览及原文", "structured_answer", None),
        ("宁德时代25年股价最高是多少", "unsupported", "2025"),
        ("问的是最高", "unsupported", "2025"),
        ("那最低呢？", "unsupported", "2025"),
        ("那24年的呢？", "unsupported", "2024"),
        ("宁德时代2025年股价最高是多少", "unsupported", "2025"),
        ("那PE呢", "unsupported", "2025"),
        ("宁德时代最新股价是多少", "market_data", None),
        ("宁德时代2025年营业收入是多少", "structured_answer", None),
        ("那最新PE呢", "market_data", None),
    ]
    started = time.perf_counter()
    try:
        with httpx.Client(base_url=origin, auth=auth, trust_env=False, timeout=45,
                          follow_redirects=False,
                          headers={"Origin": origin, "ngrok-skip-browser-warning": "1"}) as client:
            health = client.get("/api/health")
            checks["new_runtime_ready"] = (
                health.status_code == 200 and health.json().get("deployment", {}).get(
                    "market_version") == VERSION
            )
            if not checks["new_runtime_ready"]:
                raise RuntimeError("Service has not loaded the fixed runtime")
            created = client.post("/api/sessions")
            created.raise_for_status()
            sid = created.json()["session_id"]
            report["test_session_id"] = sid
            for question, mode, year in cases:
                begin = time.perf_counter()
                response = client.post("/api/chat", json={"session_id": sid, "question": question})
                response.raise_for_status()
                answer = response.json()
                passed = answer["mode"] == mode and not answer.get("usage")
                if mode == "unsupported":
                    passed &= (year in answer["message"]
                               and "不能用最新报价代替" in answer["message"]
                               and not answer.get("sources") and not answer.get("market"))
                elif mode == "market_data":
                    passed &= (answer["market"]["kind"] == "quote"
                               and not answer["resolved"]["market_intent"]["years"])
                else:
                    passed &= bool(answer.get("claims")) and bool(answer.get("sources"))
                rows.append({"question": question, "passed": bool(passed), "expected_mode": mode,
                             "seconds": time.perf_counter() - begin, "answer": answer})
            history = client.get(f"/api/sessions/{sid}")
            checks["all_test_turns_persisted"] = (
                history.status_code == 200 and len(history.json()["turns"]) == len(cases)
            )
    except Exception as error:
        report["error_type"] = type(error).__name__
        checks["completed_all_requests"] = False
    finally:
        after, _ = snapshot()
        report["after"] = after
        checks["release_files_unchanged"] = before["runtime"] == after["runtime"]
        checks["release_still_matches_proof"] = after["runtime_matches_release"]
        checks["qa_data_unchanged"] = before["qa"] == after["qa"]
        baseline = json.loads((ROOT / "docs/retrospective/evidence/phase28-market-fix-before.json"
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
                      denominator=len(cases), passed=sum(r["passed"] for r in rows),
                      all_passed=(len(rows) == len(cases) and all(r["passed"] for r in rows)
                                  and all(checks.values())),
                      timing_scope="initialized serial HTTP incl. provider IO; excludes restart",
                      regression="fresh sessions; old turns retained; no sealed evaluation rerun")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"report": str(args.output), "passed": report["passed"],
                          "denominator": report["denominator"], "seconds": report["seconds"],
                          "failed_checks": [k for k, v in checks.items() if not v],
                          "failed_questions": [r["question"] for r in rows if not r["passed"]],
                          "error_type": report.get("error_type")}, ensure_ascii=False))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
