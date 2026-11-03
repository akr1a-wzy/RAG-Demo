"""Build an explicit allowlisted source review archive; never a runtime/data backup."""

import argparse
import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / "src/app/web/static/downloads"
OUTPUT = ROOT / "output/source/youju-source-review.zip"
SUFFIXES = {".py", ".js", ".html", ".css", ".svg", ".json", ".sh", ".ps1"}
README = """# 有据 · RAG-Demo：源码阅读包

本包用于作品集代码评审，包含实现、测试、配置、依赖锁及选定评测摘要。
它不是完整运行环境：不含原始PDF、模型、索引、审核目录/证据图、封存题目和报告，
因此解压后不能直接启动完整Demo，也不能把原环境{test_count}项测试通过视为本包全部测试可直接执行。

## 阅读顺序

1. src/app/web/api.py：FastAPI网页、会话与来源PDF白名单。
2. src/app/web/service.py：员工生成、核准字段和行情工具的分流。
3. src/app/qa/workflow.py、facts.py、catalog.py：检索生成、事实绑定与核准目录。
4. src/app/retrieval/query_tasks.py、published.py：任务检索与发布凭证校验。
5. src/app/web/public.py：精确域名/同源、访问码、独立会话及保守请求额度。
6. tests/：测试源码；部分测试依赖未随包提供的原始数据与冻结报告。
7. review/metrics.json：选定报告的口径、样本量、分母与原报告SHA256。

## 运行前提

原项目使用Python3.11和uv.lock；默认GPU检索，不自动降级CPU。
需另外准备相同哈希的PDF、BGE模型、已发布Chroma索引、运行凭证以及核准目录/证据图。
目录和来源校验具有强版本绑定，不能拿任意PDF或重新生成的索引冒充原验收版本。
DeepSeek Key仅写入本机.env；.env.example是空密钥模板。

依赖准备示例：uv sync --extra gpu --locked（仅原Windows GPU依赖路径，其他平台需单独验证）。
资料准备完成后，在本机运行Start-LocalDemo.ps1。该命令不是本阅读包的完整恢复流程。
财务和公告使用核准事实直接列值或Decimal计算；员工走RAG并保留草稿。

## 结果边界

v4的93.33%是30道可回答题上的指定来源页Recall@5，不是答案准确率。
85/85是已见业务回归，同一助手审核；403项是选定事实证据检查，非全表语义验收。
2024年值来自2025年报比较列；旧失败和复杂表头风险保留，不承诺任意问题均可准确回答。
最新行情路由已保留短年度/极值条件与相邻纠正追问；年度最高/最低价的取数和计算尚未实现。
不支持的年度行情明确提示，不能用最新报价或无关文档证据代替。

## 材料与隐私

未打包.env、ngrok配置、访问码、会话库、日志、下载缓存、模型、PDF、向量与原始实验响应。
GitHub作品集仓库名为RAG-Demo；是否已公开以在线项目页的实际链接为准。
本包不附加开源许可证，未作额外授权声明。
SOURCE_MANIFEST.json记录包内文件SHA256，供核对文件完整性。
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def secret_values():
    # Only compare locally; never emit, hash into a report, or ship actual credentials.
    values = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if re.search(r"KEY|TOKEN|PASSWORD|SECRET", key, re.I) and len(value) >= 8:
                values.append(value.encode())
    access = ROOT / "tmp/public-demo/access.txt"
    if access.exists():
        for line in access.read_text(encoding="utf-8").splitlines():
            if line.startswith("Password: "):
                values.append(line.removeprefix("Password: ").encode())
    for key, value in os.environ.items():
        if re.search(r"(?:API_KEY|AUTH_TOKEN|AUTHTOKEN|PASSWORD)$", key, re.I) and len(value) >= 8:
            values.append(value.encode())
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path,
                        default=ROOT / "eval/results/portfolio_source_audit.json")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report already exists; choose a new report path")
    latest_path = ROOT / "docs/retrospective/evidence/phase28-market-fix-final.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    entries = {}
    for folder in ["src", "scripts", "tests", "config"]:
        for path in (ROOT / folder).rglob("*"):
            if (not path.is_file() or path.suffix.lower() not in SUFFIXES
                    or "__pycache__" in path.parts or "downloads" in path.parts
                    or path.is_symlink()):
                continue
            relative = path.relative_to(ROOT).as_posix()
            entries[relative] = path.read_bytes()
    for name in ["pyproject.toml", "uv.lock", ".python-version", ".env.example", ".gitignore",
                 "Start-LocalDemo.ps1", "Start-PublicDemo.ps1", "Stop-PublicDemo.ps1",
                 "Change-DemoAccessCode.ps1"]:
        entries[name] = (ROOT / name).read_bytes()
    entries["README.md"] = README.replace("{test_count}", str(latest["tests"]["passed"])).encode()
    retrieval_path = ROOT / "eval/results/blind_v4_once.json"
    retrieval = json.loads(retrieval_path.read_text(encoding="utf-8"))["summary"]
    business_path = ROOT / "eval/results/development/catalog-demo-v3/report.json"
    business = json.loads(business_path.read_text(encoding="utf-8"))
    metrics = {
        "snapshot_date": "2026-11-03", "generated_answer_accuracy_accepted": False,
        "retrieval_v4": {
            "role": "one_shot_source_page_retrieval_not_answer_accuracy", "top_k": 5,
            "answerable_count": retrieval["answerable_count"],
            "mean_source_page_recall_at_5": retrieval["mean_source_page_recall_at_5"],
            "all_sources_hit_count": retrieval["all_sources_hit_count"],
            "refusal_not_evaluated": retrieval["refusal_not_evaluated"],
            "original_report": retrieval_path.relative_to(ROOT).as_posix(),
            "original_report_sha256": digest(retrieval_path.read_bytes()),
        },
        "business_v3": {
            "role": business["role"], "same_assistant_review": business["same_assistant_review"],
            "passed": business["passed"], "denominator": business["denominator"],
            "groups": business["groups"], "seconds": business["seconds"],
            "timing_scope": business["timing_scope"], "top_k_employee": 5,
            "top_k_catalog": None,
            "original_report": business_path.relative_to(ROOT).as_posix(),
            "original_report_sha256": digest(business_path.read_bytes()),
        },
        "code_tests": {"snapshot_date": "2026-11-03", "passed": latest["tests"]["passed"],
                       "role": "original_local_environment_suite_not_archive_reproduction"},
        "market_constraints": {
            "version": latest["version"], "annual_extremes_supported": False,
            "bounded_correction_and_constraint_protection": True,
            "known_local_http": latest["local_http"], "known_public_http": latest["public_http"],
            "model_calls": 0, "original_report": latest_path.relative_to(ROOT).as_posix(),
            "original_report_sha256": digest(latest_path.read_bytes()),
        },
        "corpus": {"companies": 20, "documents": 60, "pages": 5364, "vectors": 32668},
        "audited_selected_facts": {"financial": 320, "announcement_related": 83,
                                   "total": 403, "whole_corpus_semantics_verified": False},
    }
    entries["review/metrics.json"] = (
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n").encode()
    # Defense in depth over the allowlist, including text templates and generated summaries.
    forbidden = {"data", "eval", "tmp", ".tmp", ".cloud", ".git", ".venv", "output"}
    secrets = secret_values()
    for name, content in entries.items():
        if Path(name).parts[0] in forbidden or name == ".env" or ".." in Path(name).parts:
            raise ValueError(f"Disallowed archive path: {name}")
        if any(value in content for value in secrets):
            raise ValueError(f"Local credential matched in candidate archive file: {name}")
        if re.search(rb"sk-[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{20,}", content):
            raise ValueError(f"Potential credential literal in candidate archive file: {name}")
    manifest = {"role": "source_review_only", "snapshot_date": "2026-11-03",
                "files": {name: {"sha256": digest(value), "bytes": len(value)}
                          for name, value in sorted(entries.items())}}
    entries["SOURCE_MANIFEST.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, value in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    with zipfile.ZipFile(OUTPUT) as archive:
        assert archive.testzip() is None
        for name, expected in manifest["files"].items():
            assert digest(archive.read(name)) == expected["sha256"]
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    published = DOWNLOADS / OUTPUT.name
    shutil.copyfile(OUTPUT, published)
    report = {"archive": str(OUTPUT), "published": str(published), "files": len(entries),
              "bytes": OUTPUT.stat().st_size, "sha256": digest(OUTPUT.read_bytes()),
              "allowlist_and_manifest_passed": True, "credential_scan_passed": True,
              "forbidden_directories_absent": True, "full_demo_reproducible_from_archive": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
