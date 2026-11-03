"""串行下载巨潮公开披露文件；逐文件校验、登记，重复运行可恢复。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = [
    ("600036", "招商银行", "银行"),
    ("601318", "中国平安", "保险"),
    ("600900", "长江电力", "电力"),
    ("601088", "中国神华", "煤炭"),
    ("601857", "中国石油", "石油"),
    ("600028", "中国石化", "石油化工"),
    ("600030", "中信证券", "证券"),
    ("000333", "美的集团", "家电"),
    ("000651", "格力电器", "家电"),
    ("002594", "比亚迪", "汽车"),
    ("300750", "宁德时代", "电池"),
    ("600276", "恒瑞医药", "医药"),
    ("600887", "伊利股份", "乳制品"),
    ("601012", "隆基绿能", "光伏"),
    ("002415", "海康威视", "电子设备"),
    ("600050", "中国联通", "通信"),
    ("600019", "宝钢股份", "钢铁"),
]
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.cninfo.com.cn/new/disclosure",
    "Origin": "https://www.cninfo.com.cn",
}
MANIFEST = ROOT / "data/manifests/documents.jsonl"
CHECKPOINT = ROOT / "data/manifests/expansion_checkpoint.json"
IDENTITIES = ROOT / "data/manifests/expansion_companies.json"
ORG_CACHE = {}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def request_bytes(url, payload=None):
    for attempt in range(1, 5):
        # 所有网络请求串行限速；超时后退避，不并发冲击披露接口。
        time.sleep(1)
        headers = dict(HEADERS)
        if payload is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url, data=urllib.parse.urlencode(payload).encode() if payload else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as error:
            print(f"RETRY {attempt}/4 {url}: {type(error).__name__}: {error}", flush=True)
            if getattr(error, "code", None) in (400, 401, 403, 404):
                raise
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def resolve_company(code):
    if not ORG_CACHE and IDENTITIES.exists():
        ORG_CACHE.update(json.loads(IDENTITIES.read_text(encoding="utf-8")))
    if code not in ORG_CACHE:
        rows = json.loads(request_bytes("https://www.cninfo.com.cn/new/information/topSearch/query", {
            "keyWord": code, "maxNum": "10",
        }))
        matches = [r for r in rows if r.get("code") == code and r.get("category") == "A股"
                   and str(r.get("delisted")).lower() == "false"]
        if len(matches) != 1:
            raise ValueError(f"{code}: 无法确认唯一未退市A股标识")
        ORG_CACHE[code] = matches[0]
        atomic_json(IDENTITIES, ORG_CACHE)
    return ORG_CACHE[code]


def query_page(code, keyword, page=1):
    sh = code.startswith("6")
    identity = resolve_company(code)
    data = json.loads(request_bytes("https://www.cninfo.com.cn/new/hisAnnouncement/query", {
        "stock": f"{code},{identity['orgId']}",
        "tabName": "fulltext", "pageSize": "30", "pageNum": str(page),
        "column": "sse" if sh else "szse", "plate": "sh" if sh else "sz",
        "category": "", "seDate": "2026-01-01~2026-10-12",
        "searchkey": keyword, "secid": "", "sortName": "", "sortType": "",
        "isHLtitle": "true",
    }))
    result = []
    for row in data.get("announcements") or []:
        if str(row.get("secCode")) != code:
            continue
        row["clean_title"] = html.unescape(re.sub(r"<[^>]+>", "", row["announcementTitle"]))
        if str(row.get("adjunctUrl", "")).lower().endswith(".pdf"):
            result.append(row)
    return result, int(data.get("totalpages") or 1)


def query(code, keyword):
    result, pages = query_page(code, keyword)
    if pages > 30:
        raise ValueError(f"{code}: {keyword} 超过30页，需缩小检索条件")
    for page in range(2, pages + 1):
        rows, _ = query_page(code, keyword, page)
        result.extend(rows)
    return result


def is_annual(title):
    return bool(re.search(r"2025\s*年?\s*(?:年度报告|年报)(?:全文|正文)?(?:[（(].*?[）)])?$", title)) and not any(
        s in title for s in ("摘要", "英文", "审计", "H股", "H 股", "平安银行", "平安人寿", "平安产险")
    )


def select(code):
    rows = query(code, "2025")
    # 完整年报必须排除摘要、英文版、审计报告和其他附属材料。
    annual = [r for r in rows if is_annual(r["clean_title"])]
    if not annual:
        rows += query(code, "年度报告")
        annual = [r for r in rows if is_annual(r["clean_title"])]
    if not annual:
        raise ValueError(f"{code}: 未找到2025完整年报，不以摘要替代")
    chosen = [("annual", max(annual, key=lambda r: r["announcementTime"]))]
    rows += query(code, "利润分配")
    distribution = [r for r in rows if "2025" in r["clean_title"]
                    and any(s in r["clean_title"] for s in ("利润分配", "分红派息", "权益分派"))
                    and not any(s in r["clean_title"] for s in ("半年度", "中期", "季度", "英文"))]
    plans = [r for r in distribution if "实施" not in r["clean_title"]
             and any(s in r["clean_title"] for s in ("预案", "方案", "不进行", "不分配"))]
    implementations = [r for r in distribution if "实施公告" in r["clean_title"]]
    if not implementations:
        more = query(code, "权益分派") + query(code, "分红派息")
        implementations = [r for r in more if "2025" in r["clean_title"] and "实施公告" in r["clean_title"]
                           and not any(s in r["clean_title"] for s in ("半年度", "中期", "季度"))]
    if plans:
        chosen.append(("dividend-plan", max(plans, key=lambda r: r["announcementTime"])))
    if implementations:
        chosen.append(("dividend-implementation", max(implementations, key=lambda r: r["announcementTime"])))
    if len(chosen) < 3:
        meetings = query(code, "股东会") + query(code, "股东大会")
        for suffix, kind in (("决议公告", "annual-shareholders-resolution"), ("通知", "annual-shareholders-notice")):
            for row in meetings:
                title = row["clean_title"]
                if "2025" in title and suffix in title and "年度" in title and len(chosen) < 3:
                    if row["announcementId"] not in {r["announcementId"] for _, r in chosen}:
                        chosen.append((kind, row))
    if len(chosen) != 3:
        raise ValueError(f"{code}: 只选到{len(chosen)}份合格文件，需要核查备选公告")
    return chosen


def validate_pdf(content):
    if not content.startswith(b"%PDF"):
        raise ValueError("响应不是PDF")
    with fitz.open(stream=content, filetype="pdf") as document:
        characters = sum(len(page.get_text()) for page in document)
        if characters < 100:
            raise ValueError("无足够可提取文字，拒绝作为文字版资料")
        return len(document), characters


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=17)
    parser.add_argument("--codes", nargs="+", choices=[c for c, _, _ in COMPANIES], help="只续跑指定公司")
    args = parser.parse_args()
    existing = [json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {r["document_id"]: r for r in existing}
    state = json.loads(CHECKPOINT.read_text(encoding="utf-8")) if CHECKPOINT.exists() else {"selections": {}, "failures": {}}
    backup = MANIFEST.with_name("documents.before-expansion-2026-10-12.jsonl")
    if not backup.exists():
        backup.write_bytes(MANIFEST.read_bytes())
    for code, name, industry in COMPANIES[:args.limit]:
        if args.codes and code not in args.codes:
            continue
        try:
            print(f"COMPANY {code} {name}", flush=True)
            identity = resolve_company(code)
            if identity["zwjc"] != name:
                raise ValueError(f"证券简称变化: {name} -> {identity['zwjc']}")
            if code not in state["selections"]:
                state["selections"][code] = [{"kind": kind, "row": row} for kind, row in select(code)]
                atomic_json(CHECKPOINT, state)
            for selection in state["selections"][code]:
                kind, row = selection["kind"], selection["row"]
                document_id = f"{code}-2025-{kind}"
                if document_id in by_id:
                    record = by_id[document_id]
                    content = (ROOT / record["local_path"]).read_bytes()
                    if hashlib.sha256(content).hexdigest() != record["sha256"]:
                        raise ValueError(f"已有文件哈希不匹配: {document_id}")
                    validate_pdf(content)
                    print(f"REUSE {document_id}", flush=True)
                    continue
                title = row["clean_title"]
                filename = f"{code}_{name}_{kind}_2025.pdf"
                target = ROOT / "data/raw" / code / filename
                url = "https://static.cninfo.com.cn/" + row["adjunctUrl"].lstrip("/")
                if target.exists():
                    content = target.read_bytes()
                else:
                    content = request_bytes(url)
                pages, characters = validate_pdf(content)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    temporary = target.with_suffix(".pdf.part")
                    temporary.write_bytes(content)
                    temporary.replace(target)
                record = {
                    "document_id": document_id, "stock_code": code, "stock_name": name,
                    "document_type": "annual_report" if kind == "annual" else "announcement",
                    "title": title, "report_period": "2025",
                    "publish_date": row["adjunctUrl"].split("/")[1],
                    "announcement_id": str(row["announcementId"]), "source_url": url,
                    "local_path": target.relative_to(ROOT).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
                    "page_count": pages, "extracted_characters": characters,
                }
                # 仅追加已校验记录，不重写旧9条。中断后可按ID续跑。
                with MANIFEST.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                by_id[document_id] = record
                print(f"OK {document_id} pages={pages} chars={characters}", flush=True)
            state["failures"].pop(code, None)
        except Exception as error:
            state["failures"][code] = f"{type(error).__name__}: {error}"
            print(f"FAILED {code}: {state['failures'][code]}", flush=True)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(CHECKPOINT, state)
    records = list(by_id.values())
    report = {
        "updated_at": state.get("updated_at"), "companies": len({r["stock_code"] for r in records}),
        "documents": len(records), "pages": sum(r["page_count"] for r in records),
        "new_documents": len(records) - len([l for l in backup.read_text(encoding="utf-8").splitlines() if l.strip()]),
        "failures": state["failures"], "indexed": False,
    }
    atomic_json(ROOT / "data/manifests/expansion_report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 1 if state["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
