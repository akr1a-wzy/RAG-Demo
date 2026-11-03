"""验收工作流状态契约及两道旧拒答；强制无生成请求，不评答案准确率。"""

import json
import os
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    from pydantic import SecretStr

    from app.qa.config import QASettings
    from app.qa.deepseek import DeepSeekClient
    from app.qa.workflow import DocumentQA
    from app.retrieval.incremental import RETRIEVAL_PROFILE
    from app.retrieval.pdf_extract import file_sha256
    from app.retrieval.published import open_published_retriever, read_json

    profile = read_json(ROOT / RETRIEVAL_PROFILE)
    qa = DocumentQA(
        lambda: open_published_retriever(ROOT),
        DeepSeekClient(QASettings(deepseek_api_key=SecretStr(""))),
        supported_codes=set(profile["companies"]), supported_periods={"2025"},
    )
    dataset = ROOT / "eval/answer_contract_v1.jsonl"
    cases = [json.loads(s) for s in dataset.read_text(encoding="utf-8").splitlines() if s.strip()]
    old = [json.loads(s) for s in (ROOT / "eval/dataset.jsonl").read_text(
        encoding="utf-8").splitlines() if s.strip()]
    cases.extend({"id": c["id"], "question": c["question"], "expected_mode": "refused",
                  "history": c.get("conversation", [])} for c in old if not c["answerable"])
    results = []
    start = perf_counter()
    for case in cases:
        response = qa.ask(case["question"], case["history"])
        results.append({"id": case["id"], "expected_mode": case["expected_mode"],
                        "actual_mode": response["mode"],
                        "passed": response["mode"] == case["expected_mode"],
                        "source_count": len(response.get("sources", [])),
                        "resolved": response["resolved"]})
    report = {
        "version": "answer-state-contract-v1", "dataset_sha256": file_sha256(dataset),
        "cases": results, "passed_count": sum(c["passed"] for c in results),
        "denominator": len(results), "generation_api_calls": 0,
        "top_k": 5, "candidate_k_per_task": 20,
        "collection": profile["collection_name"], "profile_id": profile["release_id"],
        "old_refusal_passed": sum(c["passed"] for c in results if c["id"].startswith("eval-")),
        "old_refusal_denominator": 2, "seconds_including_runtime_init": perf_counter() - start,
        "answer_accuracy": None,
        "scope": (
            "Known workflow-state and rule-refusal contract; no generated answer or "
            "independent refusal generalization acceptance"
        ),
    }
    target = ROOT / "eval/results/phase6_answer_contract.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False))
    if report["passed_count"] != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
