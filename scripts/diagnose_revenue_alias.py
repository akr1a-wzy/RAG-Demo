"""Freeze reported revenue-alias failures and offline catalog controls without API writes."""

import hashlib
import json
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.market.service import plan_market_request  # noqa: E402
from app.qa.catalog import VERSION, DocumentCatalog, parse_request  # noqa: E402


def main():
    evidence = ROOT / "docs/retrospective/evidence"
    output = evidence / "phase30-revenue-alias-diagnosis.json"
    if output.exists():
        raise FileExistsError("Do not overwrite frozen diagnosis evidence")
    reported = ["总营收", "25年总营收", "宁德时代2025年总营收"]
    database = ROOT / "data/public_demo/sessions.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        anchor = db.execute(
            "SELECT id,session_id FROM turns WHERE question=? ORDER BY id DESC LIMIT 1",
            (reported[-1],),
        ).fetchone()
        if not anchor:
            raise ValueError("Reported original session not found")
        rows = db.execute(
            "SELECT id,question,result_json,created_at FROM turns "
            "WHERE session_id=? AND id<=? ORDER BY id", (anchor[1], anchor[0]),
        ).fetchall()
    original = [r for r in rows if r[1] in reported][-3:]
    if [r[1] for r in original] != reported:
        raise ValueError("The three reported questions are not in the expected order")
    first = original[0][0]
    history = [{"role": "user", "content": r[1]} for r in rows if r[0] < first][-20:]
    catalog = DocumentCatalog(ROOT)
    probes = []
    started = time.perf_counter()
    for question in reported + [
        "宁德时代2025年营业收入", "宁德时代2025年营收",
        "宁德时代25年营收", "宁德时代2025年营业总收入",
    ]:
        request = parse_request(question, history)
        answer = catalog.ask(question, history)
        probes.append({"question": question, "market_request": plan_market_request(
            question, history), "request": request, "answer": answer})
    seconds = time.perf_counter() - started
    assert all(p["market_request"] is None for p in probes)
    assert [p["request"]["unrecognized"] for p in probes[:3]] == ["总", "25总", "总"]
    assert all(p["answer"]["mode"] == "structured_answer" for p in probes[3:5])
    files = ["src/app/qa/catalog.py", "src/app/qa/resolution.py",
             "src/app/retrieval/chroma_store.py", "src/app/market/service.py",
             "src/app/web/service.py", "tests/unit/test_document_catalog.py"]
    archive_path = evidence / "phase30-revenue-alias-source.zip"
    if archive_path.exists():
        with zipfile.ZipFile(archive_path) as archive:
            assert all(archive.read(rel) == (ROOT / rel).read_bytes() for rel in files)
    else:
        with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED) as archive:
            for rel in files:
                archive.write(ROOT / rel, rel)
    screenshot = Path(
        "C:/Users/ADMIN/AppData/Local/Temp/codex-clipboard-5e77ae46-c9d9-465b-8216-dc66b97a10f7.png"
    )
    if screenshot.is_file():
        shutil.copyfile(screenshot, evidence / "phase30-user-revenue-alias.png")
    report = {
        "date": datetime.now(UTC).isoformat(), "version": VERSION,
        "role": "offline_known_failure_diagnosis_not_answer_accuracy",
        "sample_count": len(probes), "denominator": len(probes), "top_k": None,
        "seconds": seconds,
        "timing_scope": "seven parser/catalog calls; excludes imports, DB IO and catalog PDF audit",
        "model_calls": 0, "network_calls": 0, "session_writes": 0, "fixed": False,
        "history": history,
        "original_turns": [{"id": i, "question": q, "answer": json.loads(r), "created_at": t}
                           for i, q, r, t in original],
        "source_hashes": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files},
        "probes": probes,
        "outcomes": {mode: sum(
            (p["answer"] or {}).get("mode", "not_catalog_request") == mode for p in probes
        ) for mode in ["structured_answer", "unsupported", "not_catalog_request"]},
        "limitations": ("No new provider/API/UI execution or production changes; "
                        "not a new acceptance"),
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"report": str(output), "seconds": seconds, "probes": [
        {"question": p["question"],
         "unrecognized": (p["request"] or {}).get("unrecognized"),
         "mode": (p["answer"] or {}).get("mode", "not_catalog_request")}
        for p in probes]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
