"""Offline reproduction of the reported annual-high and corrective-followup failure.

No provider calls, model calls, application startup, or session writes.
Run: ./.venv/Scripts/python.exe -X utf8 scripts/diagnose_market_highest.py
"""

import hashlib
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.market.service import RUNTIME_VERSION, plan_market_request  # noqa: E402
from app.qa.resolution import resolve_document_query  # noqa: E402


def main():
    output = ROOT / "docs/retrospective/evidence/phase27-market-highest-diagnosis.json"
    if output.exists():
        raise FileExistsError(
            "Frozen failure evidence exists; use verify_market_constraint_http.py"
        )
    original = "宁德时代25年股价最高是多少"
    followup = "问的是最高"
    db_path = ROOT / "data/public_demo/sessions.sqlite"
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT id, session_id FROM turns WHERE question=? ORDER BY id DESC LIMIT 1",
            (followup,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Reported followup not found; refusing to invent session evidence")
        turn_id, session_id = row
        previous = db.execute(
            "SELECT question FROM turns WHERE session_id=? AND id<? ORDER BY id",
            (session_id, turn_id),
        ).fetchall()
        history = [{"role": "user", "content": r[0]} for r in previous][-20:]
        if not history or history[-1]["content"] != original:
            raise RuntimeError(
                "Latest matching session does not have the reported original question"
            )
        rows = db.execute(
            "SELECT id, question, result_json, created_at FROM turns "
            "WHERE session_id=? AND id IN (?,?) ORDER BY id",
            (session_id, turn_id - 1, turn_id),
        ).fetchall()
    cases = [
        ("original_with_history", original, history[:-1], "reject_historical"),
        ("original_without_history", original, [], "reject_historical"),
        ("four_digit_year", "宁德时代2025年股价最高是多少", [], "reject_historical"),
        ("corrective_followup", followup, history, "preserve_market_intent"),
        ("highest_without_period", "宁德时代股价最高是多少", [], "avoid_generic_quote"),
        ("latest_quote_control", "宁德时代最新股价是多少", [], "latest_quote"),
    ]
    started = time.perf_counter()
    probes = []
    for label, question, context, contract in cases:
        request = plan_market_request(question, context)
        if contract == "reject_historical":
            passed = request is not None and request["status"] in {"unsupported", "clarification"}
        elif contract == "preserve_market_intent":
            passed = request is not None
        elif contract == "avoid_generic_quote":
            passed = request is not None and not (
                request["status"] == "supported" and request["kind"] == "quote"
            )
        else:
            passed = (request is not None and request["status"] == "supported"
                      and request["kind"] == "quote")
        probes.append({"label": label, "question": question, "history": context,
                       "contract": contract, "passed": passed, "request": request,
                       "resolved": resolve_document_query(question, context)})
    seconds = time.perf_counter() - started
    files = ["src/app/market/service.py", "src/app/web/service.py",
             "src/app/qa/sessions.py", "src/app/qa/resolution.py",
             "src/app/retrieval/chroma_store.py", "src/app/web/static/app.js"]
    report = {
        "date": datetime.now(UTC).isoformat(), "version": RUNTIME_VERSION,
        "role": "offline_known_failure_diagnosis_not_answer_accuracy",
        "sample_count": len(probes), "denominator": len(probes),
        "passed": sum(p["passed"] for p in probes), "top_k": None,
        "seconds": seconds,
        "timing_scope": "six planner/resolver probes only; excludes imports and DB IO",
        "model_calls": 0, "network_calls": 0, "session_writes": 0,
        "source_hashes": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files},
        "persisted_turns": [{"id": i, "question": q, "result": json.loads(r), "created_at": t}
                            for i, q, r, t in rows],
        "probes": probes, "fixed": False,
        "regression": ("No production changes; latest-quote and four-digit-year controls only. "
                       "No sealed evaluation rerun."),
        "limitations": ("No fresh provider/API/UI execution; persisted original responses "
                        "and current offline routing verified."),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "passed": report["passed"],
                      "denominator": len(probes), "seconds": seconds,
                      "fixed": False,
                      "probes": [{"label": p["label"], "passed": p["passed"],
                                  "status": p["request"]["status"] if p["request"] else None,
                                  "kind": p["request"]["kind"] if p["request"] else None,
                                  "period": p["resolved"]["report_period"]} for p in probes]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
