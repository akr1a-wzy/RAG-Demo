"""Prepare an audited GitHub source-review snapshot, without accessing any Git credentials."""

import argparse
import ast
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from build_portfolio_source import secret_values

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "output/github/RAG-Demo"
README = """# 有据 · RAG-Demo

面向复杂年报的可追溯问答项目，重点处理PDF结构、混合检索、事实约束、引用和失败回归。

[在线项目介绍](https://headwear-caliber-demanding.ngrok-free.dev/static/project.html) ·
[体验Demo](https://headwear-caliber-demanding.ngrok-free.dev/) ·
[4页项目说明](docs/project.pdf) · [架构](docs/architecture.md) · [运行边界](docs/reproduction.md)

在线Demo需要演示账号与另行提供的访问码，本仓库不包含凭据。服务运行于本机GPU，经ngrok转发，
设备或网络离线时Demo不可用；本仓库是源码阅读快照，不是完整运行环境。

## 实现范围

- 20家公司、60份公开年报与公告、5,364页PDF，32,668个向量子片段。
- 员工问答：BM25＋BGE向量＋RRF、任务/字段检索、GPU重排、DeepSeek生成与事实/引用校验，输出仍为草稿。
- 财务/公告：读取核准目录，使用Decimal执行受支持的换算、同比和差额，保留原年度、单位与PDF物理页。
- 行情：最新股价、PE/PB/市值、最近交易日前复权日K和显式快照回放。
- FastAPI聊天页、会话恢复、PDF来源、独立公网认证与持久化请求额度。

## 最新修复与仍未实现

`tencent-market-qfq-v1.3-constraint-routing`保留“25年/2025年、最高/最低”及相邻纠正追问，
不再用最新报价替代年度极值，也不把“问的是最高”送去查无关年报。
指定年度完整行情取数、全年覆盖验证、极值计算尚未实现；不支持时明确提示。
其他复杂表头、任意财务语义和自然问法泛化仍有边界，旧错误响应与失败报告不改写。

## 证据与指标

| 检查 | 结果 | 口径 |
|---|---:|---|
| v4指定来源页Recall@5 | 93.33% | 30道可回答题，完整来源找齐26/30；不是答案准确率 |
| 已知业务回归 | 85/85 | 同一助手审核，已见题；不同契约不能直接比较提升 |
| 选定事实证据检查 | 403项 | 320条财务比较事实＋83条公告相关事实，非全表语义验收 |
| 最新行情约束HTTP | 本机/公网各10/10 | 已知场景、0生成；未实现年度极值功能 |
| 原本机完整测试 | 567项通过 | 2026-11-03，2条已有依赖警告；不是本仓库解压复现结果 |

选定指标角色、分母及原报告SHA见[review/metrics.json](review/metrics.json)。原始数据、审核证据及封存题目不随本阅读包公开，
本仓库不把本机实验写成线上SLA、生产流量或通用准确率。

## 从哪里读代码

1. [Web入口](src/app/web/api.py)与[业务分流](src/app/web/service.py)。
2. [问答图](src/app/qa/workflow.py)、[事实绑定](src/app/qa/facts.py)、
   [核准字段](src/app/qa/catalog.py)。
3. [任务检索](src/app/retrieval/query_tasks.py)与[发布凭证](src/app/retrieval/published.py)。
4. [行情及约束](src/app/market/service.py)与[相邻追问测试](tests/unit/test_market_constraints.py)。
5. [公网入口](src/app/web/public.py)及其认证、同源和额度测试。

## 本仓库可以直接做什么

使用Python3.11执行`python tools/check_source.py`，校验文件清单、哈希和Python语法，
不需要模型或API Key。
完整Demo还依赖未随包提供的PDF、模型、冻结索引、发布凭证、核准目录及图像证据。
依赖与准备边界详见[docs/reproduction.md](docs/reproduction.md)，不能承诺克隆后直接启动或执行全部567项测试。

材料快照：2026-11-03。项目名称暂保留“有据”；GitHub仓库名由用户指定为RAG-Demo。
"""
ARCHITECTURE = """# 实际执行路径

员工问题：恢复公司/年份/主体 → BM25和向量召回/RRF → 任务与字段补召回 → GPU重排Top5
→ DeepSeek → 事实/年度/单位/引用检查 → 草稿。

核准财务与公告：受限意图解析 → 审核字段目录 → 直接列值或Decimal计算 → 原文引用。
不把这些结果混入检索Recall或LLM生成准确率。

行情问题：保留期间与操作 → 检查支持范围 → 独立行情工具 → 来源/业务时间/获取时间。
显式回放必须标非实时。
年度最高/最低价在取数前返回不支持，相邻受限纠正保留年度；最新行情明确清除旧年度，文档切换切断继承。

公网入口只包装原API，使用独立会话库、访问码、精确Host/Origin、请求体限制和持久化尝试额度。
用户的历史轮次与冻结失败报告不因修复回写。索引、模型、策略及发布凭证通过哈希绑定；故障不静默换CPU。
"""
REPRODUCTION = """# 运行与复现边界

这是可审阅的实现快照，包含源码、测试源码、配置与依赖锁。需要Python3.11；原完整环境为Windows GPU。

可以独立运行：`python tools/check_source.py`。
该检查不需要第三方依赖，只检查本快照完整性和Python语法。

完整Demo还需：

1. 根据原文档清单准备匹配SHA256的PDF，保留来源与报告期间。
2. 准备固定revision的BGE中文嵌入与重排模型。
3. 恢复已发布Chroma集合、切片及运行凭证；不能随意改文件来套用旧验收。
4. 准备核准字段目录和对应坐标/图像审核证据；不能绕过审核或直接套评测金标准。
5. 在本机`.env`配置生成Key；空模板为`.env.example`，凭据不提交。

以上PDF、模型、索引、文档清单、审核目录、报告与封存评测数据没有包含在本仓库。
部分测试依赖这些输入，不能承诺克隆本仓库后原567项全部可执行；未提供经过验收的一键恢复包。

原Windows GPU依赖准备命令为`uv sync --extra gpu --locked`。输入完整后才可运行`Start-LocalDemo.ps1`。
Linux/CPU部署及重新构建索引需单独验收，不能直接沿用原GPU结果。

2024财务值来自本库2025年报比较列，保留调整与重述口径。全表语义、任意新问法与年度最高/最低价没有完成全面验收。
"""
CHECKER = '''"""Offline integrity and Python-syntax check for the published source snapshot."""
import ast
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
checked = 0
for name, record in manifest["files"].items():
    path = root / name
    assert path.resolve().is_relative_to(root.resolve()), name
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == record["sha256"], name
    if path.suffix == ".py":
        ast.parse(data, filename=name)
    checked += 1
assert not (root / ".env").exists(), "Real .env must not be published"
print(f"Checked {checked} file hashes and Python syntax; no runtime/model/API calls")
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=TARGET)
    parser.add_argument("--report", type=Path,
                        default=ROOT / "eval/results/phase29-github-candidate-audit.json")
    args = parser.parse_args()
    target_dir = args.output_dir.resolve()
    if not target_dir.is_relative_to((ROOT / "output/github").resolve()):
        parser.error("Candidate must stay within output/github")
    if args.report.exists():
        parser.error("Audit report exists; choose a new report")
    if target_dir.exists():
        raise SystemExit("Candidate exists; choose/review it rather than overwriting")
    target_dir.mkdir(parents=True)
    with zipfile.ZipFile(ROOT / "output/source/youju-source-review.zip") as archive:
        for name in archive.namelist():
            target = (target_dir / name).resolve()
            if not target.is_relative_to(target_dir):
                raise ValueError("Unsafe archive member")
            if name == "SOURCE_MANIFEST.json":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    (target_dir / "README.md").write_text(README, encoding="utf-8")
    (target_dir / "docs").mkdir()
    (target_dir / "docs/architecture.md").write_text(ARCHITECTURE, encoding="utf-8")
    (target_dir / "docs/reproduction.md").write_text(REPRODUCTION, encoding="utf-8")
    shutil.copyfile(ROOT / "output/pdf/youju-project.pdf", target_dir / "docs/project.pdf")
    (target_dir / "tools").mkdir()
    (target_dir / "tools/check_source.py").write_text(CHECKER, encoding="utf-8")
    sensitive = secret_values()
    files = {}
    for path in target_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        name = path.relative_to(target_dir).as_posix()
        if any(value in data for value in sensitive):
            raise ValueError(f"Credential match in candidate: {name}")
        if path.suffix == ".py":
            ast.parse(data, filename=name)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    manifest = {"role": "public_source_review_not_full_runtime", "date": "2026-11-03",
                "files": dict(sorted(files.items()))}
    (target_dir / "SOURCE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"candidate": str(target_dir), "files": len(files) + 1, "secrets_scan_passed": True,
              "runtime_data_excluded": True, "syntax_checked": True,
              "total_bytes": sum(f["bytes"] for f in files.values())}
    with args.report.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
