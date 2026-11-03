"""本机完整Demo新问法/业务验收；prepare冻结，run一次，原v1/v2/v4不重跑。"""

import argparse
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import httpx
from evaluate_answers_once import reserve_attempt
from evaluate_answers_v2_once import aggregate_v2

from app.evaluation.answers import normalized
from app.qa.config import QASettings

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "eval/local_demo_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    if BASE.exists():
        raise ValueError("已存在验收目录，不覆盖")
    gold = read(ROOT / "eval/answer_acceptance_v2_gold.json")
    prior = read(ROOT / "eval/answer_acceptance_v2_freeze.json")["file_hashes"]
    for fact in gold["facts"]:
        assert sha(ROOT / fact["pdf_path"]) == fact["pdf_sha256"]
        image = fact["screenshot"]
        assert sha(ROOT / image) == prior[image]
    cases = []

    def add(key, group, question, codes=(), *, metric="headcount_total", mode="draft", **kwargs):
        cases.append({"id": key, "group": group, "question": question, "expected_mode": mode,
                      "fact_ids": [f"{code}:2025:{metric}" for code in codes], **kwargs})

    for f in gold["facts"]:
        if f["metric"] != "headcount_total":
            continue
        name = "平安体系内" if f["stock_code"] == "601318" else f["stock_name"]
        scope = "本公司" if f["stock_code"] == "600028" else ""
        qualifier = "（含派遣人员）" if f["stock_code"] == "600036" else ""
        add("staff-" + f["stock_code"], "new_staff_single",
            f"请核对{name}2025年报列示的{scope}在职员工总人数{qualifier}，给出原文引用和人数。",
            [f["stock_code"]])
    for i, codes, question in [
        (1, ["000333", "000651"], "请对照美的集团和格力电器2025年报，分别列在职员工总人数与出处。"),
        (2, ["600036", "600030"],
         "请核对2025年招商银行（含派遣人员）、中信证券各自员工人数和引用。"),
    ]:
        add(f"comparison-{i}", "staff_comparison", question, codes)
    for i, code, question, depends in [
        (1, "600887", "接着查伊利股份，在职员工总人数及出处呢？", "staff-000858"),
        (2, "002415", "再查海康威视同一年度的员工人数和出处。", "staff-000651"),
        (3, "600019", "保持2025年，宝钢股份的员工总人数及原文呢？", "staff-600900"),
        (4, "600030", "同一家公司，给出2025年的在职员工总人数和引用。", "staff-600030"),
    ]:
        add(f"follow-{i}", "staff_followup", question, [code], depends_on=depends)
    for code, question in [
        ("000858", "请核对五粮液2025年度营业收入，给出原始单位及引用。"),
        ("300750", "麻烦给出宁德时代2025年全年营收和原始单位。"),
        ("600276", "请查询恒瑞医药2025年合并口径营业收入及出处。"),
        ("600887", "只看伊利股份，给出它2025年营业收入及引用。"),
    ]:
        add("finance-" + code, "financial_answerable", question, [code], metric="revenue")
    for i, condition in enumerate([
        "拥有滑翔伞执照", "养鹦鹉", "会四门外语", "参加过沙漠马拉松",
        "收藏陨石", "左手写字", "有航海经验", "学过古希腊语",
    ]):
        add(f"refusal-{i}", "no_evidence_refusal",
            f"五粮液2025年在职员工中，{condition}的有多少人？请给出处。", mode="refused")
    for i, question, mode, extra in [
        (1, "请核对2025年在职员工总人数。", "clarification", {}),
        (2, "请核对隆基绿能在职员工总人数与出处。", "clarification", {}),
        (3, "美的集团2020年员工总人数及出处？", "refused", {}),
        (4, "999999公司2025年的在职员工总人数？", "refused", {}),
        (5, "中国神华未来股价会涨到哪里？", "refused", {}),
        (6, "宁德时代2025年母公司营业收入及出处？", "evidence_only", {}),
        (7, "五粮液2025年度营业收入是多少？", "evidence_only", {"evidence_only": True}),
        (8, "平安2025年员工总人数及原文？", "clarification", {}),
    ]:
        add(f"control-{i}", "routing_control", question, mode=mode, **extra)
    for i, question, codes, kind, days, mode, extra in [
        (1, "贵州茅台最新价格和PE、PB、市值请列出。", ["600519"], "quote", 20, "market_data", {}),
        (2, "再查这家公司的最新市盈率与市净率。", ["300750"], "quote", 20, "market_data",
         {"depends_on": "staff-300750"}),
        (3, "中国神华和贵州茅台最新价格与总市值分别是多少？", ["601088", "600519"],
         "quote", 20, "market_data", {}),
        (4, "五粮液最近20个交易日的日K线及涨跌幅。", ["000858"], "kline", 20, "market_data", {}),
        (5, "宁德时代最近250个交易日的前复权K线及收益。", ["300750"],
         "kline", 250, "market_data", {}),
        (6, "仍看五粮液最近20个交易日K线与涨跌幅。", ["000858"], "kline", 20, "market_data",
         {"market_replay": True}),
        (7, "贵州茅台2024年股价是多少？", [], "quote", 20, "unsupported", {}),
        (8, "中国神华最近251个交易日的日K线。", [], "kline", 251, "unsupported", {}),
    ]:
        add(f"market-{i}", "market_control", question, mode=mode, market_codes=codes,
            market_kind=kind, market_days=days, **extra)
    seen = set()
    for name in ["eval/answer_acceptance_v1.jsonl", "eval/answer_acceptance_v2.jsonl"]:
        seen.update(normalized(json.loads(s)["question"]) for s in (ROOT / name).read_text(
            encoding="utf-8").splitlines())
    assert len({normalized(c["question"]) for c in cases}) == len(cases)
    assert not [c["id"] for c in cases if normalized(c["question"]) in seen]
    contract = read(ROOT / "eval/answer_acceptance_v2_contract.json")
    contract.update(version="local-demo-v1-known-facts-new-questions", planned_counts={"total": 54},
                    scope="Scoped local demo workflows; not unrestricted financial QA",
                    novelty=("54 texts distinct from prior v1/v2 questions; 24 known facts; "
                             "same assistant; no personnel or fact isolation"),
                    gold_standard=("Reuse original visual PDF gold; recheck PDF/image SHA; "
                                   "no new visual review of every page"),
                    main_answer_denominator=30, staff_answer_denominator=26,
                    promotion="All output stays draft; scoped set is not universal answer accuracy")
    BASE.mkdir()
    for name, data in [("gold.json", gold), ("contract.json", contract)]:
        (BASE / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (BASE / "cases.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n"
                                              for c in cases), encoding="utf-8")
    previous = read(ROOT / "eval/results/phase17_qa_freeze.json")
    paths = set(previous["file_hashes"]) | {p.relative_to(ROOT).as_posix()
        for d in ["src/app/market", "src/app/web"] for p in (ROOT / d).rglob("*")
        if p.is_file() and p.suffix in {".py", ".html", ".js", ".css"}}
    paths |= {"scripts/accept_local_demo.py", "scripts/verify_market_live.py",
              "tests/unit/test_market_demo.py", "tests/integration/test_web_api.py"}
    paths |= {p.relative_to(ROOT).as_posix() for p in BASE.iterdir()}
    with httpx.Client(trust_env=False) as client:
        health = client.get("http://127.0.0.1:8000/api/health").json()
    assert health["status"] == "ready" and health["deployment"]["market_version"]
    freeze = {"file_hashes": {p: sha(ROOT / p) for p in sorted(paths)}, "runtime": health,
              "protected_hashes": previous["protected_hashes"]}
    (BASE / "freeze.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps({"cases": len(cases), "frozen": len(paths), "new_facts": 0}))


def _market_check(case, answer):
    if answer.get("mode") != case["expected_mode"] or answer.get("claims") or answer.get("usage"):
        return False
    if case["expected_mode"] != "market_data":
        return True
    market = answer.get("market", {})
    if market.get("data_mode") != ("replay" if case.get("market_replay") else "live"):
        return False
    datasets = market.get("datasets", [])
    if {d["quote"]["code"] for d in datasets} != set(case["market_codes"]):
        return False
    for data in datasets:
        q, chart = data["quote"], data["chart"]
        keys = ["price", "pe_ttm", "pb", "market_cap_yi", "as_of"]
        if not all(q.get(k) is not None for k in keys):
            return False
        bars = chart["bars"]
        expected = (Decimal(bars[-1]["close"]) / Decimal(chart["baseline"]["close"]) - 1) * 100
        if (len(bars) != case["market_days"] or chart["internal_bar_count"] != len(bars) + 1
                or chart["adjustment"] != "qfq" or not data.get("fetched_at")
                or Decimal(chart["return_percent"]) != expected.quantize(Decimal("0.0001"))):
            return False
    return True


def market_check(case, answer):
    try:
        return _market_check(case, answer)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def run():
    if any((BASE / n).exists() for n in ["state.json", "raw.jsonl", "report.json"]):
        raise RuntimeError("已有一次状态/结果，禁止重跑")
    freeze, cases = read(BASE / "freeze.json"), [json.loads(s) for s in (
        BASE / "cases.jsonl").read_text(encoding="utf-8").splitlines()]

    def verify():
        for p, h in {**freeze["file_hashes"], **freeze["protected_hashes"]}.items():
            assert sha(ROOT / p) == h, p

    verify()
    secret = QASettings(_env_file=ROOT / ".env").deepseek_api_key.get_secret_value()
    assert secret
    records, sessions = [], {}
    state = {"status": "running", "attempt_count": 1, "planned": len(cases)}
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=120) as client:
        assert client.get("/api/health").json()["deployment"] == freeze["runtime"]["deployment"]
        reserve_attempt(BASE / "state.json", state)
        started = perf_counter()
        try:
            with (BASE / "raw.jsonl").open("x", encoding="utf-8") as out:
                for c in cases:
                    begin = perf_counter()
                    try:
                        sid = sessions.get(c.get("depends_on"))
                        if not sid:
                            created = client.post("/api/sessions")
                            created.raise_for_status()
                            sid = created.json()["session_id"]
                        sessions[c["id"]] = sid
                        r = client.post("/api/chat", json={"session_id": sid,
                            "question": c["question"],
                            "evidence_only": c.get("evidence_only", False),
                            "market_replay": c.get("market_replay", False)})
                        r.raise_for_status()
                        answer = r.json()
                    except (httpx.HTTPError, ValueError) as error:
                        answer = {"mode": "transport_error", "error_type": type(error).__name__}
                    record = {"id": c["id"], "question": c["question"], "response": answer,
                              "http_seconds": perf_counter() - begin}
                    encoded = json.dumps(record, ensure_ascii=False)
                    assert secret not in encoded
                    out.write(encoded + "\n")
                    out.flush()
                    os.fsync(out.fileno())
                    records.append(record)
                    print(json.dumps({"done": len(records), "id": c["id"],
                                      "mode": answer.get("mode")}), flush=True)
            verify()
            assert client.get("/api/health").json()["deployment"] == freeze["runtime"]["deployment"]
            state["status"] = "evaluated_once"
        except Exception as error:
            state.update(status="interrupted_no_retry", error_type=type(error).__name__)
    report = aggregate_v2(records, cases, read(BASE / "gold.json"), read(BASE / "contract.json"))
    by_id = {r["id"]: r["response"] for r in records}
    checks = [{"id": c["id"], "passed": market_check(c, by_id.get(c["id"], {}))}
              for c in cases if c["group"] == "market_control"]
    report.update(role="one_shot_local_workflow_new_wording_on_known_facts_same_assistant",
                  market_checks=checks, total_seconds=perf_counter() - started,
                  generated_answer_accuracy_accepted=False, completed_state=state["status"])
    report["gates"]["market_contracts_100"] = all(c["passed"] for c in checks) and len(checks) == 8
    report["gate_passed"] = all(report["gates"].values()) and state["status"] == "evaluated_once"
    with (BASE / "report.json").open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    state.update(completed=len(records), gate_passed=report["gate_passed"],
                 report_sha256=sha(BASE / "report.json"), raw_sha256=sha(BASE / "raw.jsonl"))
    (BASE / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["metrics", "gates", "gate_passed", "usage"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "run"])
    args = parser.parse_args()
    prepare() if args.mode == "prepare" else run()
