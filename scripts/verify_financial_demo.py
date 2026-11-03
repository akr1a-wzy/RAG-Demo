"""本机财务证据与边界HTTP检查；不调用生成接口，只清空本脚本新建会话。"""

import argparse
import json
from pathlib import Path
from time import perf_counter

import httpx

from app.qa.config import QASettings
from app.qa.financial import QA_POLICY

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("不覆盖已有HTTP报告")
    cases = [(f"{name}2025年营业收入多少？", True, True) for name in [
        "五粮液", "宁德时代", "恒瑞医药", "伊利股份",
    ]] + [(question, False, False) for question in [
        "宁德时代2025年营业收入多少元？",
        "宁德时代2025年营业收入同比增长多少？",
        "宁德时代2025年第二季度营业收入多少？",
        "宁德时代2025年母公司营业收入多少？",
        "宁德时代2025年营业收入换算成亿元是多少？",
        "宁德时代2025年营业总收入多少？",
        "中国神华2025年营业收入多少？",
        "宁德时代2025年净利润多少？",
        "宁德时代与中国神华2025年营业收入分别多少？",
    ]]
    start, records = perf_counter(), []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ready"
        assert health["deployment"]["qa_evidence_policy"] == QA_POLICY
        for question, evidence_only, expect_fact in cases:
            created = client.post("/api/sessions")
            created.raise_for_status()
            sid = created.json()["session_id"]
            response = client.post("/api/chat", json={
                "session_id": sid, "question": question, "evidence_only": evidence_only,
            })
            response.raise_for_status()
            answer = response.json()
            passed = answer["mode"] == "evidence_only" and not answer["claims"] and not answer[
                "usage"]
            if expect_fact:
                passed &= answer["fact_selection"]["status"] == "ready"
                passed &= any(s["facts"] for s in answer["sources"])
            records.append({"question": question, "response": answer, "passed": bool(passed)})
        history = client.get(f"/api/sessions/{sid}").json()
        reset = client.delete(f"/api/sessions/{sid}/messages")
        after = client.get(f"/api/sessions/{sid}").json()
        home = client.get("/")
        checks = {"history": len(history["turns"]) == 1,
                  "own_session_reset": reset.status_code == 200 and not after["turns"],
                  "home_scope": "年度营业收入" in home.text and "宁德" in home.text,
                  "docs": client.get("/docs").status_code == 200,
                  "new_example": any("宁德" in x["label"] for x in client.get(
                      "/api/examples").json()["examples"]),
                  "pdf_range": client.get("/documents/300750-2025-annual.pdf", headers={
                      "Range": "bytes=0-99"}).status_code == 206}
    report = {"date": "2026-10-26", "cases": records, "health": health,
              "passed": sum(r["passed"] for r in records), "denominator": len(records),
              "endpoint_checks": checks, "model_calls": 0,
              "initialized_local_http_seconds": perf_counter() - start,
              "scope": "HTTP evidence policy and metadata, not generated answer acceptance",
              "gate_passed": all(r["passed"] for r in records) and all(checks.values())}
    secret = QASettings(_env_file=ROOT / ".env").deepseek_api_key.get_secret_value()
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    assert not secret or secret not in encoded
    with args.output.open("x", encoding="utf-8") as out:
        out.write(encoded + "\n")
    print(json.dumps({k: report[k] for k in ["passed", "denominator", "endpoint_checks",
                                           "model_calls", "gate_passed"]}))
    if not report["gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
