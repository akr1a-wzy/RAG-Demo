import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    from app.retrieval.embedding_prepare import prepare_file
    from app.retrieval.embeddings import LocalEmbeddingModel

    manifest = json.loads((ROOT / "data/models/embedding_model.json").read_text(encoding="utf-8"))
    model_path = ROOT / manifest["local_path"]
    model = LocalEmbeddingModel(model_path)
    model_limit = model.model.max_seq_length
    budget = model.token_budget

    report = prepare_file(
        ROOT / "data/processed/chunks.jsonl",
        ROOT / "data/processed/pages.jsonl",
        ROOT / "data/processed/embedding_chunks.jsonl",
        ROOT / "eval/dataset.jsonl",
        model.count_tokens,
        manifest["model_id"],
        manifest["revision"],
        budget,
    )
    report["model_max_tokens"] = model_limit
    target = ROOT / "eval/results/embedding_preparation_report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        key: value
        for key, value in report.items()
        if key
        not in (
            "original_29_details",
            "evidence_validation",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告={target}")


if __name__ == "__main__":
    main()
