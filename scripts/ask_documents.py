"""本机有证据边界的文档问答；支持持久会话和显式证据模式。"""

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    from app.qa.config import QASettings
    from app.qa.deepseek import DeepSeekClient
    from app.qa.sessions import SessionStore
    from app.qa.workflow import DocumentQA
    from app.retrieval.incremental import RETRIEVAL_PROFILE
    from app.retrieval.published import open_published_retriever, read_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--session-id")
    parser.add_argument("--reset-session", action="store_true")
    parser.add_argument("--evidence-only", action="store_true", help="即使有Key也不发起生成请求")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = QASettings(_env_file=ROOT / ".env")
    if args.evidence_only:
        from pydantic import SecretStr

        settings.deepseek_api_key = SecretStr("")
    db = settings.qa_session_db
    store = SessionStore(db if db.is_absolute() else ROOT / db)
    session_id = args.session_id or store.create()
    if args.reset_session:
        store.reset(session_id)
    profile = read_json(ROOT / RETRIEVAL_PROFILE)
    artifact = ROOT / profile["artifact_root"]
    # 版本元数据只从已发布语料读取；不读取任何评测gold。
    chunk_path = artifact / "data/processed/embedding_chunks.jsonl"
    periods = {json.loads(line)["report_period"]
               for line in chunk_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    qa = DocumentQA(lambda: open_published_retriever(ROOT), DeepSeekClient(settings),
                    supported_codes=set(profile["companies"]), supported_periods=periods)
    answer = {"session_id": session_id, **qa.ask(args.question, store.history(session_id))}
    store.append(session_id, args.question, answer)
    target = args.output or ROOT / "eval/results/latest_answer.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(answer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(answer["message"])
    for claim in answer.get("claims", []):
        print(claim["text"])
        for citation in claim["citations"]:
            source = next(s for s in answer["sources"] if s["source_id"] == citation["source_id"])
            print(f"  [{citation['source_id']}] {source['title']} PDF第{source['physical_page']}页")
    if answer["mode"] == "evidence_only":
        for source in answer["sources"]:
            print(f"[{source['source_id']}] {source['title']} PDF第{source['physical_page']}页")
    print(f"mode={answer['mode']} session={session_id}\n完整结果={target}")


if __name__ == "__main__":
    main()
