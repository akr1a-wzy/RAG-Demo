"""登记本轮已完成的助手视觉复核；逐证据重读通过后发布，旧候选/失败不覆盖。"""

import json
from pathlib import Path

from app.qa.catalog import VERSION, sha, verify_facts

ROOT = Path(__file__).resolve().parents[1]


def main():
    documents = {
        d["document_id"]: d
        for d in map(
            json.loads,
            (ROOT / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines(),
        )
    }
    catalogs = []
    all_facts = []
    for kind, candidate in [
        ("document", "document_catalog.v2.candidate7.json"),
        ("announcement", "announcement_catalog.v2.candidate3.json"),
    ]:
        payload = json.loads((ROOT / "data/qa" / candidate).read_text(encoding="utf-8"))
        for index, f in enumerate(payload["facts"]):
            if kind == "document":
                pages = {p["page"] for p in f["proofs"]}
                paths = [
                    ROOT
                    / "docs/retrospective/evidence/phase20-financial-pages"
                    / f"{f['stock_code']}-p{n}.png"
                    for n in sorted(pages)
                ]
            else:
                paths = [
                    ROOT
                    / "docs/retrospective/evidence/phase20-announcement-crops"
                    / f"{index:03}.png"
                ]
            f["visual_evidence"] = [
                {"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p)} for p in paths
            ]
            f["review_status"] = "assistant_visual_verified"
            f["review_scope"] = "selected_cell_and_headers_only"
            f["reviewer"] = "same_assistant_original_pdf_visual_and_geometry"
        output = ROOT / f"data/qa/{kind}_catalog.v2.json"
        if output.exists():
            raise ValueError("Already published; use a new version")
        payload.update(version=VERSION, source_candidate=candidate)
        all_facts.extend(payload["facts"])
        catalogs.append((output, payload))
    audit = verify_facts(ROOT, all_facts, documents)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    if not audit["passed"]:
        raise ValueError("Proof audit failed")
    for path, payload in catalogs:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    release = {
        "version": VERSION,
        "catalogs": [
            {"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p)} for p, _ in catalogs
        ],
        "audit": audit,
        "review_boundary": "same assistant; selected fields, not all PDF tables",
    }
    (ROOT / "data/qa/catalog_release.v2.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ROOT / "eval/results/phase20_catalog_proof_audit.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
