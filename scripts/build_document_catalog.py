"""从原PDF几何与独立文本构建待视觉复核的财务/公告事实，不读取评测金标准。"""

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import pymupdf
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "600519": [6],
    "000858": [7],
    "000568": [7],
    "600036": [14, 15],
    "600028": [5, 6],
    "601088": [8, 9],
    "600030": [19, 20],
    "000333": [9, 10],
    "002594": [11],
    "300750": [11],
    "601318": [14],
    "600900": [6, 7],
    "601857": [8],
    "000651": [7],
    "600276": [6],
    "600887": [7],
    "601012": [13, 14],
    "002415": [9],
    "600050": [6, 7],
    "600019": [6, 7],
}
ALIASES = {
    "revenue": ["营业收入"],
    "net_profit_parent": [
        "归属于上市公司股东的净利润",
        "归属于母公司股东的净利润",
        "归属于本公司股东的净利润",
        "归属于本行股东的净利润",
    ],
    "net_profit_parent_excl": [
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "归属于母公司股东的扣除非经常性损益后的净利润",
        "归属于母公司股东的扣除非经常性损益的净利润",
        "归属于本公司股东的扣除非经常性损益的净利润",
        "扣除非经常性损益后归属于本行股东的净利润",
    ],
    "operating_cashflow": ["经营活动产生的现金流量净额"],
    "total_assets": ["总资产", "资产总额", "资产总计"],
    "net_assets_parent": [
        "归属于上市公司股东的净资产",
        "归属于母公司股东权益",
        "归属于母公司股东的权益",
        "归属于本行股东的权益",
        "归属于本行股东权益",
        "归属于本公司股东的权益",
        "归属于本公司股东权益",
        "归属于本公司股东的净资产",
        "归属于母公司股东的净资产",
    ],
    "basic_eps": ["基本每股收益", "归属于本行普通股股东的基本每股收益"],
    "roe": ["加权平均净资产收益率", "归属于本行普通股股东的加权平均净资产收益率"],
}
NUM = re.compile(r"\(?[-+]?\d[\d,]*(?:\.\d+)?%?\)?$")


def compact(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s)).replace("−", "-")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def characters(page):
    chars, boxes = [], []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for c in span["chars"]:
                    for ch in unicodedata.normalize("NFKC", c["c"]):
                        if not ch.isspace():
                            chars.append(ch)
                            boxes.append(c["bbox"])
    return "".join(chars), boxes


def rect(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def headers(page):
    result = []
    for a in page.search_for("2025"):
        bs = [b for b in page.search_for("2024") if abs(a.y0 - b.y0) < 16 and b.x0 - a.x1 > 20]
        if bs and a.x0 > 95:
            b = min(bs, key=lambda b: b.x0)
            result.append(
                {
                    "bbox": [a.x0, a.y0 + 1, a.x1, a.y1 - 1],
                    "x": (a.x0 + a.x1) / 2,
                    "next_x": (b.x0 + b.x1) / 2,
                    "next_bbox": [b.x0, b.y0 + 1, b.x1, b.y1 - 1],
                    "y": a.y1,
                }
            )
    return result


def financial_rows(document, pdf):
    records, rejected, previous = [], [], None
    code = document["stock_code"]
    independent = PdfReader(ROOT / document["local_path"])
    for n in PAGES[code]:
        page = pdf[n - 1]
        text, boxes = characters(page)
        words = page.get_text("words")
        hs = headers(page)
        if previous and n == previous["page"] + 1:
            # 人工确认的摘要跨页只继承同一网格；逐行数值仍须落在原列中心。
            hs = [{**h, "y": 0, "page": previous["page"]} for h in previous["headers"][-1:]] + hs
        pypdf_text = compact(independent.pages[n - 1].extract_text() or "")
        matches = []
        stops = [
            rect(boxes[m.start() : m.end()])[1]
            for m in re.finditer(r"分季度|第?一季度|非经常性损益项目", text)
        ]
        for metric, aliases in ALIASES.items():
            for alias in sorted(aliases, key=len, reverse=True):
                for m in re.finditer(re.escape(alias), text):
                    if any(a <= m.start() and b >= m.end() for a, b, _ in matches):
                        continue
                    if (
                        metric == "basic_eps"
                        and "扣除非经常性损益" in text[max(0, m.start() - 30) : m.start()]
                    ):
                        continue
                    if (
                        metric == "roe"
                        and "扣除非经常性损益" in text[max(0, m.start() - 30) : m.start()]
                    ):
                        continue
                    matches.append((m.start(), m.end(), metric))
        matches = [
            m
            for m in matches
            if not any(a <= m[0] and b >= m[1] and b - a > m[1] - m[0] for a, b, _ in matches)
        ]
        for start, end, metric in sorted(matches):
            label_box = rect(boxes[start:end])
            if re.search(r"每股$|平均$", text[max(0, start - 4) : start]):
                continue
            if metric == "revenue" and re.search(r"收入后的$", text[max(0, start - 8) : start]):
                continue
            if re.match(r"回报率|报酬率|周转|收益率", text[end : end + 8]):
                continue
            valid_headers = [
                h for h in hs if h["y"] < label_box[1] + 2 and label_box[0] < h["x"] - 30
            ]
            if not valid_headers:
                continue
            h = max(valid_headers, key=lambda h: h["y"])
            if any(h["y"] < y < label_box[1] for y in stops):
                continue
            if any(
                h["y"] < r.y0 < label_box[1]
                for phrase in ["分季度", "第一季度", "非经常性损益项目"]
                for r in page.search_for(phrase)
            ):
                continue
            nums = [
                w
                for w in words
                if NUM.fullmatch(compact(w[4]))
                and label_box[1] - 2 <= (w[1] + w[3]) / 2 <= label_box[3] + 2
                and (w[0] + w[2]) / 2 > label_box[0] + 40
            ]
            current = [w for w in nums if abs((w[0] + w[2]) / 2 - h["x"]) < 45]
            if len(current) != 1:
                rejected.append(
                    {
                        "code": code,
                        "page": n,
                        "metric": metric,
                        "reason": "ambiguous_current_column",
                        "values": [w[4] for w in current],
                    }
                )
                continue
            first = current[0]
            values = sorted([w for w in nums if w[0] >= first[0] - 1], key=lambda w: w[0])
            if len(values) < 2:
                continue
            # 非财务段落、单位或注号不能借用年度列。
            if abs((first[0] + first[2]) / 2 - h["x"]) > (h["next_x"] - h["x"]) * 0.48:
                continue
            row_box = [
                max(0, label_box[0] - 1),
                label_box[1] - 1,
                page.rect.width - 25,
                max(label_box[3], first[3]) + 1,
            ]
            quote = page.get_textbox(pymupdf.Rect(row_box)).strip()
            if compact(first[4]) not in pypdf_text:
                rejected.append(
                    {
                        "code": code,
                        "page": n,
                        "metric": metric,
                        "reason": "independent_value_missing",
                    }
                )
                continue
            nearby = page.get_textbox(
                pymupdf.Rect(0, max(0, label_box[1] - 80), page.rect.width, label_box[3] + 2)
            )
            unit_matches = []
            for u in ["单位:百万元", "单位:千元", "单位:元", "人民币百万元", "人民币元"]:
                for m in re.finditer(re.escape(u), text):
                    if re.search(r"每股.{0,8}$|收益\($", text[max(0, m.start() - 16) : m.start()]):
                        continue
                    ub = rect(boxes[m.start() : m.end()])
                    if ub[1] < label_box[1]:
                        unit_matches.append((ub[3], u, ub))
            raw_label = text[start:end]
            suffix = text[end : end + 15]
            if metric == "basic_eps":
                unit = "元/股"
            elif metric == "roe":
                unit = "%"
            elif re.match(r"\((?:人民币)?(?:千|百万元|元)", suffix):
                match = re.match(r"\((?:人民币)?(百万元|千元|元)", suffix)
                unit = match[1] if match else None
            else:
                unit = None
            if metric not in {"basic_eps", "roe"} and not unit:
                context = compact(nearby)
                units = re.findall(r"单位[:：](?:人民币)?(百万元|千元|元)", context)
                if units:
                    unit = units[-1]
                elif unit_matches:
                    candidate = max(unit_matches)[1]
                    unit = (
                        "百万元"
                        if "百万元" in candidate
                        else "千元"
                        if "千元" in candidate
                        else "元"
                    )
                elif previous and not any(u in compact(nearby) for u in ["美元", "港元"]):
                    unit = previous.get("unit")
            if not unit:
                rejected.append({"code": code, "page": n, "metric": metric, "reason": "no_unit"})
                continue
            unit_box = max(unit_matches)[2] if unit_matches else row_box
            unit_page = n
            if metric in {"basic_eps", "roe"}:
                unit_box = row_box
                marker = "元" if metric == "basic_eps" else "%"
                if marker not in compact(quote):
                    positions = [
                        rect(boxes[m.start() : m.end()])
                        for m in re.finditer(
                            r"每股计\(人民币元\)|人民币元|财务比率\(%\)|\(%\)", text
                        )
                    ]
                    positions = [b for b in positions if b[1] < label_box[1]]
                    if not positions:
                        raise ValueError(f"Missing ratio unit: {code}:{metric}:{n}")
                    unit_box = max(positions, key=lambda b: b[1])
            if (
                not unit_matches
                and unit not in compact(quote)
                and previous
                and metric not in {"basic_eps", "roe"}
            ):
                unit_box, unit_page = previous.get("unit_box", row_box), previous["page"]
            header_page = h.get("page", n)
            proof = [
                {"kind": "row", "page": n, "bbox": row_box, "text": quote},
                {
                    "kind": "value",
                    "page": n,
                    "bbox": [first[0], first[1] + 1, first[2], first[3] - 1],
                    "text": page.get_textbox(
                        pymupdf.Rect(first[0], first[1] + 1, first[2], first[3] - 1)
                    ),
                },
                {
                    "kind": "year",
                    "page": header_page,
                    "bbox": h["bbox"],
                    "text": pdf[header_page - 1].get_textbox(pymupdf.Rect(h["bbox"])),
                },
                {
                    "kind": "unit",
                    "page": unit_page,
                    "bbox": unit_box,
                    "text": pdf[unit_page - 1].get_textbox(pymupdf.Rect(unit_box)),
                },
            ]
            value = compact(first[4]).rstrip("%")
            if value.startswith("(") and value.endswith(")"):
                value = "-" + value[1:-1]
            records.append(
                {
                    "stock_code": code,
                    "metric": metric,
                    "period": "2025",
                    "document_id": document["document_id"],
                    "physical_page": n,
                    "value": value,
                    "unit": unit,
                    "row_label": raw_label,
                    "row_quote": quote,
                    "proofs": proof,
                    "accounting_basis": "as_reported",
                    "entity_scope": "consolidated_group",
                    "header_page": header_page,
                    "unit_page": unit_page,
                    "independent_value_verified": True,
                }
            )
            other = values[1]
            if compact(other[4]) in pypdf_text:
                previous_value = compact(other[4]).rstrip("%")
                if previous_value.startswith("(") and previous_value.endswith(")"):
                    previous_value = "-" + previous_value[1:-1]
                basis = "as_reported"
                basis_proof = []
                year_page = pdf[header_page - 1]
                for word in year_page.get_text("words"):
                    if (
                        word[4] in {"调整后", "重述后"}
                        and abs((word[0] + word[2]) / 2 - (other[0] + other[2]) / 2) < 45
                        and h["bbox"][1] <= word[1] <= h["y"] + 36
                    ):
                        basis = "adjusted" if word[4] == "调整后" else "restated"
                        basis_proof = [
                            {
                                "kind": "accounting_basis",
                                "page": header_page,
                                "bbox": list(word[:4]),
                                "text": word[4],
                            }
                        ]
                records.append(
                    {
                        **records[-1],
                        "period": "2024",
                        "value": previous_value,
                        "accounting_basis": basis,
                        "proofs": [
                            proof[0],
                            {
                                "kind": "value",
                                "page": n,
                                "bbox": [other[0], other[1] + 1, other[2], other[3] - 1],
                                "text": page.get_textbox(
                                    pymupdf.Rect(other[0], other[1] + 1, other[2], other[3] - 1)
                                ),
                            },
                            {
                                "kind": "year",
                                "page": header_page,
                                "bbox": h["next_bbox"],
                                "text": year_page.get_textbox(pymupdf.Rect(h["next_bbox"])),
                            },
                            proof[3],
                            *basis_proof,
                        ],
                    }
                )
        # 为已指定/待人工复核的摘要连续页保留局部单位，绝不写回全量切片表头。
        full = compact(page.get_text())
        um = re.findall(r"单位[:：](?:人民币)?(百万元|千元|元)", full)
        if not um:
            um = re.findall(r"人民币(百万元|千元|元)", full)
        annual_hs = [h for h in headers(page) if h["y"] < page.rect.height * 0.98]
        previous = {
            "page": n,
            "headers": annual_hs,
            "unit": um[0]
            if um
            else next(
                (
                    r["unit"]
                    for r in records
                    if r["physical_page"] == n and r["unit"] in {"元", "千元", "百万元"}
                ),
                None,
            ),
            "unit_box": next(
                (
                    list(r)
                    for s in ["单位：百万元", "单位：人民币元", "单位：元", "人民币百万元"]
                    for r in page.search_for(s)
                ),
                [0, 0, 1, 1],
            ),
        }
    return records, rejected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/qa/document_catalog.v2.candidate.json"
    )
    output = parser.parse_args().output
    if output.exists():
        raise ValueError("不覆盖已有候选")
    documents = [
        json.loads(s)
        for s in (ROOT / "data/manifests/documents.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    facts, rejected, coverage = [], [], {}
    for d in documents:
        if d["document_type"] != "annual_report":
            continue
        assert sha(ROOT / d["local_path"]) == d["sha256"]
        with pymupdf.open(ROOT / d["local_path"]) as pdf:
            rows, bad = financial_rows(d, pdf)
        for r in rows:
            r.update(
                pdf_sha256=d["sha256"],
                review_status="pending_visual_review",
                fact_id=f"{d['stock_code']}:{r['period']}:{r['metric']}:p{r['physical_page']}",
            )
        facts.extend(rows)
        rejected.extend(bad)
        coverage[d["stock_code"]] = sorted({r["metric"] for r in rows})
        print(
            json.dumps(
                {
                    "code": d["stock_code"],
                    "metrics": coverage[d["stock_code"]],
                    "missing": sorted(set(ALIASES) - set(coverage[d["stock_code"]])),
                }
            ),
            flush=True,
        )
    output.write_text(
        json.dumps(
            {"facts": facts, "rejected": rejected, "coverage": coverage},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
