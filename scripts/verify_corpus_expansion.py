"""核查扩容语料，输出审计报告；不触发Embedding或Chroma更新。"""

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = ROOT / "data/manifests/documents.jsonl"
    records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    baseline = (ROOT / "data/manifests/documents.before-expansion-2026-10-12.jsonl").read_bytes()
    historical_ids = {json.loads(line)["document_id"] for line in baseline.decode("utf-8").splitlines() if line.strip()}
    errors = []
    if not manifest.read_bytes().startswith(baseline):
        errors.append("原9条清单字节发生变化")
    if len({r["document_id"] for r in records}) != len(records):
        errors.append("document_id重复")
    companies = {}
    pdfs = []
    parser_warnings = []
    historical_count_differences = []
    # 不把可恢复的解析警告藏掉：收集到报告，避免海量重复stderr。
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    for record in records:
        code = record["stock_code"]
        companies.setdefault(code, {"stock_name": record["stock_name"], "documents": 0, "annual_reports": 0, "pages": 0})
        companies[code]["documents"] += 1
        companies[code]["annual_reports"] += record["document_type"] == "annual_report"
        companies[code]["pages"] += record["page_count"]
        try:
            path = (ROOT / record["local_path"]).resolve()
            if not path.is_relative_to(ROOT / "data/raw"):
                raise ValueError("路径不在data/raw")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("SHA256不匹配")
            if len(content) != record["size_bytes"]:
                raise ValueError("文件大小不匹配")
            if urlparse(record["source_url"]).hostname != "static.cninfo.com.cn":
                raise ValueError("非巨潮公开源")
            pymupdf.TOOLS.mupdf_warnings(reset=True)
            with pymupdf.open(stream=content, filetype="pdf") as document:
                lengths = [len(page.get_text()) for page in document]
                if len(document) != record["page_count"]:
                    raise ValueError("页数不匹配")
                if sum(lengths) != record["extracted_characters"]:
                    if record["document_id"] not in historical_ids:
                        raise ValueError("新文件原始提取字符数不匹配")
                    # 老清单未记录统计方法/版本，不能把字符数当作内容哈希。
                    # 原9条字节、PDF SHA与页数均严格校验；差异单独披露，绝不改旧值。
                    historical_count_differences.append({
                        "document_id": record["document_id"], "historical_count": record["extracted_characters"],
                        "current_raw_count": sum(lengths), "status": "historical_method_not_versioned",
                    })
                if sum(lengths) < 100:
                    raise ValueError("没有足够可提取文字")
                if record["document_type"] == "annual_report" and len(document) < 30:
                    raise ValueError("年报过短，需要排查是否摘要")
            warnings = pymupdf.TOOLS.mupdf_warnings(reset=True)
            if warnings:
                parser_warnings.append({"document_id": record["document_id"], "warning": warnings,
                                        "status": "text_extractable_but_semantics_not_verified"})
            pdfs.append({"document_id": record["document_id"], "sha256_verified": True,
                         "pages": len(lengths), "text_characters": sum(lengths),
                         "low_text_pages_under_100_chars": sum(n < 100 for n in lengths)})
        except Exception as error:
            errors.append(f"{record['document_id']}: {error}")
    if len(companies) != 20 or len(records) != 60:
        errors.append(f"数量不符: {len(companies)}家公司/{len(records)}份文件，目标20/60")
    for code, summary in companies.items():
        if summary["documents"] != 3 or summary["annual_reports"] != 1:
            errors.append(f"{code}: 每家必须1年报+2公告")
    index = json.loads((ROOT / "data/chroma/index_manifest.json").read_text(encoding="utf-8"))
    if index.get("count") != 1564 or index.get("signature") != "960b287b13212db54d4ddaa415049a17eb211371b401f213a04ec190b849d45d":
        errors.append("原检索索引版本发生变化，需要另行核查")
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(), "passed": not errors,
        "company_count": len(companies), "document_count": len(records),
        "new_document_count": len(records) - 9, "pages": sum(r["page_count"] for r in records),
        "size_bytes": sum(r["size_bytes"] for r in records), "old_manifest_byte_preserved": manifest.read_bytes().startswith(baseline),
        "companies": companies, "document_kinds": dict(Counter(r["document_id"].split("-2025-")[-1] for r in records)),
        "pdf_validation": pdfs, "errors": errors, "parser_warnings": parser_warnings,
        "historical_character_count_differences": historical_count_differences,
        "current_character_count_profile": {"library": "pymupdf", "version": pymupdf.VersionBind,
                                            "method": "page.get_text()", "sort": False, "cleaned": False},
        "active_index_unchanged_expected": index,
        "notes": "下载质量门槛不等于表格语义/答案准确率验收；未进行新增语料Embedding或Chroma入库",
    }
    target = ROOT / "data/manifests/expansion_validation.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({k: v for k, v in report.items() if k not in ("companies", "pdf_validation", "active_index_unchanged_expected")}, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
