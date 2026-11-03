"""公告的每股税前分红、实施日期及会议日期候选；保留预案/实施和A股口径。"""

import argparse
import json
import re
from datetime import date
from pathlib import Path

import pymupdf
from build_document_catalog import characters, compact, rect, sha
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DATE = r"20\d{2}(?:年|/|-)\d{1,2}(?:月|/|-)\d{1,2}日?(?=20\d{2}[年/-]|\D|$)"
LABELS = {
    "record_date": r"股权登记日",
    "ex_dividend_date": r"除权\(息\)日|除息日|除权除息日",
    "payment_date": r"现金红利发放日期?|派息日|红利发放日",
    "meeting_date": r"会议召开的日期|召开的日期时间|现场会议时间|现场会议召开时间|会议召开时间"
    r"|股东会召开的日期|会议召开的时间|股东会召开的时间|召开时间",
}


def iso(raw):
    parts = re.findall(r"\d+", raw)
    return date(*map(int, parts[:3])).isoformat()


def extract(document, pdf):
    facts, ratios = [], []
    reader = PdfReader(ROOT / document["local_path"])
    is_meeting = "shareholders" in document["document_id"]
    stage = (
        "meeting"
        if is_meeting
        else "implementation"
        if "implementation" in document["document_id"]
        else "plan"
    )
    for index, page in enumerate(pdf):
        text, boxes = characters(page)
        n = index + 1
        independent = compact(reader.pages[index].extract_text() or "")

        def add(metric, value, bbox, quote, *, page_number=n, **extra):
            facts.append(
                {
                    "document_id": document["document_id"],
                    "stock_code": document["stock_code"],
                    "physical_page": page_number,
                    "period": "2025",
                    "metric": metric,
                    "value": value,
                    "unit": "date" if metric.endswith("_date") else "元/股",
                    "entity_scope": "A_share",
                    "event_stage": stage,
                    "pdf_sha256": document["sha256"],
                    "review_status": "pending_visual_review",
                    "row_quote": quote,
                    "proofs": [{"kind": "row", "page": page_number, "bbox": bbox, "text": quote}],
                    **extra,
                }
            )

        for m in re.finditer(
            r"每(?P<base>10)?股.{0,12}?(?P<value>\d+(?:\.\d+)?)元.{0,15}?含税", text
        ):
            before = text[max(0, m.start() - 70) : m.start()]
            raw = m.group()
            if "现金" not in raw and "派" not in raw:
                continue
            if any(x in before[-20:] for x in ["红筹公司", "中期分红方案", "香港联交所"]):
                continue
            bbox = rect(boxes[m.start() : m.end()])
            quote = page.get_textbox(pymupdf.Rect(bbox)).strip()
            if compact(m["value"]) not in independent:
                continue
            score = 0
            if stage == "implementation":
                score += (
                    20
                    if re.search(r"本次实施|调整后的|变动至|本次权益分派方案|本次利润分配", before)
                    else 0
                )
                score -= 20 if re.search(r"原定|原方案|已实施|中期分红", before[-30:]) else 0
                score += len(m["value"].partition(".")[2])
            ratios.append(
                {
                    "score": score,
                    "page": n,
                    "bbox": bbox,
                    "quote": quote,
                    "value": m["value"],
                    "base": 10 if m["base"] else 1,
                }
            )
        for metric, pattern in LABELS.items():
            if is_meeting != (metric == "meeting_date"):
                continue
            for m in re.finditer(pattern, text):
                lb = rect(boxes[m.start() : m.end()])
                after = text[m.end() : m.end() + 50]
                inline = re.match(r"[:：为即]*(?P<d>" + DATE + r")", after)
                options = []
                if inline:
                    a = m.end() + inline.start("d")
                    b = m.end() + inline.end("d")
                    options = [(0, rect(boxes[a:b]), inline["d"])]
                else:
                    # 同一A股日期表的下一行按列中心关联；最后交易日不能冒充登记日。
                    for dm in re.finditer(DATE, text):
                        db = rect(boxes[dm.start() : dm.end()])
                        distance = abs((db[0] + db[2] - lb[0] - lb[2]) / 2)
                        if lb[3] <= db[1] <= lb[3] + 65 and distance < 42:
                            options.append((db[1] - lb[3] + distance, db, dm.group()))
                if not options:
                    continue
                _, db, raw = min(options, key=lambda x: x[0])
                bbox = [
                    min(lb[0], db[0]) - 1,
                    min(lb[1], db[1]) - 1,
                    max(lb[2], db[2]) + 1,
                    max(lb[3], db[3]) + 1,
                ]
                quote = page.get_textbox(pymupdf.Rect(bbox)).strip()
                if compact(raw) not in independent:
                    continue
                try:
                    value = iso(raw)
                except ValueError:
                    continue
                # 未写具体登记日的预案不能从“截至2025年末股本”等附近日期猜登记日。
                if stage == "plan" and not inline:
                    continue
                add(
                    metric,
                    value,
                    bbox,
                    quote,
                    raw_value=raw,
                    date_binding="inline" if inline else "column",
                )
        if not is_meeting:
            m = re.search(r"2025年度?拟?不进行利润分配|2025年度?不派发现金", text)
            if m:
                bbox = rect(boxes[m.start() : m.end()])
                add(
                    "dividend_policy",
                    "不进行利润分配",
                    bbox,
                    page.get_textbox(pymupdf.Rect(bbox)).strip(),
                    unit="text",
                )
    if ratios:
        best = max(ratios, key=lambda r: r["score"]) if stage == "implementation" else ratios[0]
        facts.append(
            {
                "document_id": document["document_id"],
                "stock_code": document["stock_code"],
                "physical_page": best["page"],
                "period": "2025",
                "metric": "dividend_gross",
                "value": best["value"],
                "unit": f"元/{best['base']}股",
                "share_basis": best["base"],
                "event_stage": stage,
                "entity_scope": "A_share",
                "tax_basis": "gross",
                "row_quote": best["quote"],
                "pdf_sha256": document["sha256"],
                "review_status": "pending_visual_review",
                "proofs": [
                    {
                        "kind": "row",
                        "page": best["page"],
                        "bbox": best["bbox"],
                        "text": best["quote"],
                    }
                ],
            }
        )
    unique = {}
    for fact in facts:
        key = fact["metric"]
        if key not in unique or (
            fact.get("date_binding") == "inline" and unique[key].get("date_binding") != "inline"
        ):
            unique[key] = fact
    for f in unique.values():
        f["fact_id"] = f"{f['document_id']}:{f['metric']}"
    return list(unique.values()), ratios


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data/qa/announcement_catalog.v2.candidate3.json")
    output = parser.parse_args().output
    if output.exists():
        raise ValueError("不覆盖已有候选")
    result = []
    all_ratios = []
    for d in map(
        json.loads,
        (ROOT / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines(),
    ):
        if d["document_type"] != "announcement":
            continue
        assert sha(ROOT / d["local_path"]) == d["sha256"]
        with pymupdf.open(ROOT / d["local_path"]) as pdf:
            fs, rs = extract(d, pdf)
        result.extend(fs)
        all_ratios.append({"document_id": d["document_id"], "candidates": rs})
        print(
            json.dumps(
                {
                    "id": d["document_id"],
                    "facts": [(f["metric"], f["value"], f["unit"], f["physical_page"]) for f in fs],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    output.write_text(
        json.dumps({"facts": result, "ratio_candidates": all_ratios}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
