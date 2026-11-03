"""已知事实的本机端到端回归；每个output只运行一次，封存旧验收不动。"""

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx

from app.qa.catalog import LABELS, MONEY_METRICS
from app.tools.ticker import CODE_TO_CANONICAL_NAME

ROOT = Path(__file__).resolve().parents[1]


def norm(s):
    return re.sub(r"\s+", "", s)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists; keep failed runs")
    args.output.mkdir(parents=True)
    facts = json.loads((ROOT / "data/qa/document_catalog.v2.json").read_text(encoding="utf-8"))[
        "facts"
    ]
    gold = json.loads((ROOT / "eval/local_demo_v1/gold.json").read_text(encoding="utf-8"))["facts"]
    employee = {f["stock_code"]: f for f in gold if f["metric"] == "headcount_total"}
    metrics = sorted(MONEY_METRICS)
    cases = []
    for i, (code, name) in enumerate(CODE_TO_CANONICAL_NAME.items()):
        employee_scope = "本公司" if code == "600028" else "体系" if code == "601318" else ""
        metric, year = metrics[i % 8], "2024" if i % 3 == 0 else "2025"
        fact = next(
            f for f in facts if (f["stock_code"], f["period"], f["metric"]) == (code, year, metric)
        )
        cases.extend(
            [
                {
                    "id": "finance-" + code,
                    "group": "financial_route",
                    "question": f"请告诉我{name}{year}年{LABELS[metric]}是多少？附原文。",
                    "mode": "structured_answer",
                    "fact_id": fact["fact_id"],
                },
                {
                    "id": "announcement-" + code,
                    "group": "announcement_route",
                    "question": f"{name}2025年分红是多少？",
                    "mode": "structured_answer",
                },
                {
                    "id": "staff-" + code,
                    "group": "employee_known_pdf_gold",
                    "question": f"{name}2025年{employee_scope}员工总人数及原文呢？",
                    "mode": "draft",
                    "gold": employee[code],
                },
            ]
        )
    for q, expected in [
        ("中国神华2025年营业收入同比变化多少？", "-13.21%"),
        ("茅台和五粮液2025年营收相差多少亿元？", "1283.10亿元"),
        ("隆基2025年ROE同比变化多少？", "1.92个百分点"),
        ("隆基2025年归母净利润同比多少？", "基数非正"),
        ("宝钢2025年全年分红多少？", "全年预计"),
        ("宝钢2025年下半年分红多少？", "0.18元"),
        ("五粮液2025年股权登记日是多少？", "2026-07-15"),
        ("宝钢2025年股东会日期是什么时候？", "2026-05-21"),
    ]:
        cases.append(
            {
                "id": f"business-{len(cases)}",
                "group": "calculation_or_event",
                "question": q,
                "mode": "structured_answer",
                "contains": expected,
            }
        )
    for q, mode in [
        ("茅台2025年母公司营业收入是多少？", "unsupported"),
        ("茅台2025年第一季度营收多少？", "unsupported"),
        ("茅台2025年营业收入增长原因？", "unsupported"),
        ("神华2024年调整前营业收入多少？", "unsupported"),
        ("茅台2025年税后分红是多少？", "unsupported"),
        ("招商银行2025年实施分红的股权登记日是多少？", "refused"),
        ("隆基2025年实施分红多少？", "refused"),
        ("平安2025年总资产多少？", "clarification"),
        ("茅台2023年营业收入多少？", "refused"),
        ("宝钢2025年精通三门外语的员工人数多少？", "refused"),
        ("中国石化2025年员工总人数及原文呢？", "clarification"),
        ("中国平安2025年员工总人数及原文呢？", "clarification"),
    ]:
        cases.append(
            {"id": f"boundary-{len(cases)}", "group": "boundary", "question": q, "mode": mode}
        )
    cases.extend(
        [
            {
                "id": "follow-1",
                "group": "followup",
                "session": "follow",
                "question": "宁德时代2025年营业收入多少？",
                "mode": "structured_answer",
            },
            {
                "id": "follow-2",
                "group": "followup",
                "session": "follow",
                "question": "那2024年呢？",
                "mode": "structured_answer",
                "contains": "2024年",
            },
            {
                "id": "follow-3",
                "group": "followup",
                "session": "follow",
                "question": "保持2025年，宝钢股份的员工总人数及原文呢？",
                "mode": "draft",
                "gold": employee["600019"],
            },
            {
                "id": "evidence",
                "group": "evidence_only",
                "question": "宁德2025年总资产多少？",
                "mode": "evidence_only",
                "evidence_only": True,
            },
            {
                "id": "market-replay",
                "group": "market_preserved",
                "question": "贵州茅台最近20个交易日日K和涨跌幅",
                "mode": "market_data",
                "market_replay": True,
            },
        ]
    )
    (args.output / "cases.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths = [
        *ROOT.glob("src/app/qa/*.py"),
        *ROOT.glob("src/app/web/**/*.py"),
        *ROOT.glob("src/app/web/static/*"),
        *ROOT.glob("data/qa/*v2.json"),
        Path(__file__),
    ]
    freeze = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
        if p.is_file()
    }
    (args.output / "freeze.json").write_text(json.dumps(freeze, indent=2), encoding="utf-8")
    sessions, rows, usage, started = {}, [], Counter(), time.perf_counter()
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=180, trust_env=False) as client:
        assert client.get("/api/health").json()["status"] == "ready"
        for case in cases:
            key = case.get("session", case["id"])
            if key not in sessions:
                response = client.post("/api/sessions")
                response.raise_for_status()
                sessions[key] = response.json()["session_id"]
            before = time.perf_counter()
            response = client.post(
                "/api/chat",
                json={
                    "session_id": sessions[key],
                    "question": case["question"],
                    "evidence_only": case.get("evidence_only", False),
                    "market_replay": case.get("market_replay", False),
                },
            )
            response.raise_for_status()
            answer = response.json()
            checks = {"mode": answer["mode"] == case["mode"]}
            claims, sources = answer["claims"], answer["sources"]
            text = "".join(c["text"] for c in claims)
            if case["mode"] in {"draft", "structured_answer"}:
                by_id = {s["source_id"]: s for s in sources}
                checks["validated"] = bool(answer["validation"].get("checks_passed"))
                checks["citations"] = bool(claims) and all(
                    c["citations"]
                    and all(
                        x["source_id"] in by_id
                        and norm(x["quote"]) in norm(by_id[x["source_id"]]["content"])
                        for x in c["citations"]
                    )
                    for c in claims
                )
            else:
                checks["no_claim"] = not claims
            if case.get("fact_id"):
                checks["selected_fact"] = case["fact_id"] in answer["fact_selection"].get(
                    "fact_ids", []
                )
            if case.get("contains"):
                checks["business_value"] = case["contains"] in text
            if case.get("gold"):
                gold_fact = case["gold"]
                values = {
                    Decimal(x.replace(",", ""))
                    for x in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text)
                }
                checks["pdf_gold_value"] = Decimal(gold_fact["value"].replace(",", "")) in values
                checks["pdf_gold_page"] = any(
                    s["stock_code"] == gold_fact["stock_code"]
                    and s["physical_page"] == gold_fact["physical_page"]
                    for s in sources
                )
            for k, v in answer.get("usage", {}).items():
                if isinstance(v, int):
                    usage[k] += v
            row = {
                "id": case["id"],
                "group": case["group"],
                "checks": checks,
                "passed": all(checks.values()),
                "seconds": time.perf_counter() - before,
                "answer": answer,
            }
            rows.append(row)
            with (args.output / "raw.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(case["id"], row["passed"], answer["mode"], flush=True)
    report = {
        "role": "known_facts_local_business_regression_not_blind_acceptance",
        "same_assistant_review": True,
        "financial_expected_ids_derived_from_catalog": True,
        "employee_gold": "carried_original_PDF_gold_not_runtime_fact_output",
        "denominator": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "groups": {
            g: {
                "passed": sum(r["passed"] for r in rows if r["group"] == g),
                "denominator": sum(r["group"] == g for r in rows),
            }
            for g in sorted({r["group"] for r in rows})
        },
        "usage": dict(usage),
        "model_responses_with_usage": sum(bool(r["answer"].get("usage")) for r in rows),
        "seconds": time.perf_counter() - started,
        "top_k_employee": 5,
        "top_k_catalog": None,
        "timing_scope": "local initialized serial HTTP; catalog/LLM/replay, excludes startup",
        "failed": [{"id": r["id"], "checks": r["checks"]} for r in rows if not r["passed"]],
        "generated_answer_accuracy_accepted": False,
        "old_one_shot_rerun": False,
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
