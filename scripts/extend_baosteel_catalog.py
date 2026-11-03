"""补入已视觉核对的宝钢年报分红段；全年预计与下半年拟派分开。"""

import json
import re
import zipfile
from pathlib import Path

import pymupdf
from build_document_catalog import characters, rect
from pypdf import PdfReader

from app.qa.catalog import DocumentCatalog, compact, sha, verify_facts

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "data/qa/annual_events.v2.json"
    if output.exists():
        raise ValueError("Already extended")
    catalog = DocumentCatalog(ROOT)
    document = catalog.documents["600019-2025-annual"]
    path = ROOT / document["local_path"]
    independent = compact(PdfReader(path).pages[50].extract_text())
    facts = []
    with pymupdf.open(path) as pdf:
        page = pdf[50]
        text, boxes = characters(page)
        for scope, pattern, value, label in [
            (
                "full_year",
                r"2025年度每股现金分红0.30元\(含税\)",
                "0.30",
                "全年预计，含已分配中期0.12元/股",
            ),
            (
                "second_half",
                r"2025年下半年公司拟派发现金股利0.18元/股\(含税\)",
                "0.18",
                "2025年下半年拟分配",
            ),
        ]:
            match = re.search(pattern, text)
            assert match and match.group() in independent
            bbox = rect(boxes[match.start() : match.end()])
            quote = page.get_textbox(pymupdf.Rect(bbox))
            image = ROOT / "docs/retrospective/evidence/phase20-financial-pages/600019-p51.png"
            facts.append(
                {
                    "fact_id": f"600019-2025-annual:dividend:{scope}",
                    "document_id": document["document_id"],
                    "stock_code": "600019",
                    "period": "2025",
                    "physical_page": 51,
                    "metric": "dividend_gross",
                    "value": value,
                    "unit": "元/1股",
                    "share_basis": 1,
                    "tax_basis": "gross",
                    "event_stage": "plan",
                    "distribution_scope": scope,
                    "scope_label": label,
                    "entity_scope": "A_share",
                    "row_quote": quote,
                    "proofs": [{"kind": "row", "page": 51, "bbox": bbox, "text": quote}],
                    "pdf_sha256": document["sha256"],
                    "review_status": "assistant_visual_verified",
                    "review_scope": "selected_paragraph",
                    "reviewer": "same_assistant_original_pdf_visual_and_geometry",
                    "visual_evidence": [
                        {"path": image.relative_to(ROOT).as_posix(), "sha256": sha(image)}
                    ],
                }
            )
    audit = verify_facts(ROOT, catalog.facts + facts, catalog.documents)
    assert audit["passed"], audit
    release_path = ROOT / "data/qa/catalog_release.v2.json"
    with zipfile.ZipFile(ROOT / "docs/retrospective/evidence/phase20-catalog-base.zip", "x") as z:
        z.write(release_path, "data/qa/catalog_release.v2.json")
    output.write_text(json.dumps({"facts": facts}, ensure_ascii=False, indent=2), encoding="utf-8")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    release["catalogs"].append({"path": output.relative_to(ROOT).as_posix(), "sha256": sha(output)})
    release["audit"] = audit
    release_path.write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "eval/results/phase20_catalog_final_audit.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(audit))


if __name__ == "__main__":
    main()
