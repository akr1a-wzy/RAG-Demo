import argparse
import json
from pathlib import Path

from app.retrieval.chunking import ChunkConfig, chunk_pages, validate_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="按页、标题和疑似表格切片并检查证据保留")
    parser.add_argument("--input", type=Path, default=Path("data/processed/pages.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/chunks.jsonl"))
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--overlap", type=int, default=100)
    parser.add_argument("--dataset", type=Path, default=Path("eval/dataset.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("eval/results/chunking_report.json"))
    args = parser.parse_args()
    config = ChunkConfig(args.chunk_size, args.overlap)
    stats = chunk_pages(args.input, args.output, config)
    checks = validate_chunks(args.input, args.output, args.dataset)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"stats": stats, "validation": checks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(
        f"证据保留: {checks['answerable_pass']}通过 / {checks['answerable_fail']}失败；"
        f"完整性错误: {len(checks['integrity_errors'])}；"
        f"{checks['refusal_not_evaluated']}条拒答留到问答阶段验收。"
    )
    print(f"切片={args.output.resolve()}\n报告={args.report.resolve()}")
    print(
        f"{checks['cases_with_nonliteral_keywords']}题有非逐字答案表述，"
        "见报告中的 keywords_not_literal_in_source；本步不验证计算和推理。"
    )
    if checks["integrity_errors"] or checks["answerable_fail"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
