"""对已保存HTTP回答离线重评分；保留首次报告，无检索或模型调用。"""

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

from app.evaluation.answers import score_case, summarize

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, choices=["financial-facts-v1"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    directory = ROOT / "eval/results/development" / args.run_id
    paths = {"raw": directory / "raw.jsonl", "original_report": directory / "report.json",
             "gold": ROOT / "eval/answer_acceptance_v1_gold.json",
             "cases": ROOT / "eval/answer_acceptance_v1.jsonl",
             "contract": ROOT / "eval/answer_acceptance_v1_contract.json",
             "scorer": ROOT / "src/app/evaluation/answers.py"}
    before = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in paths.items()}
    started = perf_counter()
    records = [json.loads(s) for s in paths["raw"].read_text(encoding="utf-8").splitlines()]
    cases = {c["id"]: c for c in map(json.loads, paths["cases"].read_text(
        encoding="utf-8").splitlines())}
    gold = json.loads(paths["gold"].read_text(encoding="utf-8"))
    facts = {f["fact_id"]: f for f in gold["facts"]}
    scores = [score_case(cases[r["id"]], r["response"], facts) for r in records]
    report = summarize(scores, json.loads(paths["contract"].read_text(encoding="utf-8")))
    report.update({"role": "seen_v1_development_saved_response_rescore_not_new_acceptance",
                   "run_id": args.run_id, "scores": scores, "case_count": len(records),
                   "file_hashes": {p.relative_to(ROOT).as_posix(): before[k]
                                   for k, p in paths.items()},
                   "reason": "Preserve adjacent decimal cell boundaries in citation numeric tokens",
                   "model_calls": 0, "retrieval_calls": 0,
                   "local_rescore_seconds": perf_counter() - started,
                   "new_answer_acceptance_performed": False})
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == before[k] for k, p in paths.items())
    with (args.output or directory / "report_rescored.json").open("x", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ["metrics", "gate_passed", "subgroups"]}))


if __name__ == "__main__":
    main()
