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
README = """# RAG-Demo

A 股年报问答 Demo（有据），用 Python、LangGraph 和 FastAPI 实现。回答附带原文摘录和 PDF 页码，
可在页面中打开来源。

[Demo](https://headwear-caliber-demanding.ngrok-free.dev/) ·
[项目介绍](https://headwear-caliber-demanding.ngrok-free.dev/static/project.html) ·
[项目说明 PDF](docs/project.pdf)

Demo 需要访问码，由本机通过 ngrok 提供服务，离线时无法访问。

## 功能

- **年报问答**：目前主要支持员工人数查询。BM25 和 BGE 向量混合检索，经重排后交给 DeepSeek
  生成回答，再检查事实、年度、单位和引用。
- **财务与公告查询**：从审核过的字段目录读取数值，用 `Decimal` 计算同比、差额和单位换算。
- **行情查询**：最新报价、PE、PB、市值、前复权日 K 线；支持手动回放保存的行情快照。
- **Web 界面**：连续对话、历史会话、PDF 跳转、浅色与深色主题。

当前数据覆盖 20 家公司、60 份年报及公告。财务和公告查询使用结构化目录，员工问答走 RAG，两条路径在
[架构说明](docs/architecture.md) 中分别介绍。

## 代码结构

```text
src/app/
├── retrieval/   # PDF 解析、切分、混合检索与重排
├── qa/          # 问答流程、字段目录、计算与引用检查
├── market/      # 行情接口、K 线与快照回放
└── web/         # FastAPI、会话存储和前端
scripts/         # 数据处理、校验和启动脚本
tests/           # 单元与集成测试
docs/            # 架构、运行说明和评测记录
```

请求从 [`web/service.py`](src/app/web/service.py) 分流。员工问答流程见
[`qa/workflow.py`](src/app/qa/workflow.py)，财务目录查询见
[`qa/catalog.py`](src/app/qa/catalog.py)，行情入口见
[`market/service.py`](src/app/market/service.py)。

## 运行

Python 3.11，原开发环境为 Windows + NVIDIA GPU。依赖由 `uv.lock` 固定。

**仓库未附带原始 PDF、模型、索引和审核目录，克隆后还不能直接启动完整 Demo。** 所需输入和配置见
[运行说明](docs/reproduction.md)。

不安装依赖也可以检查源码文件与 Python 语法：

```bash
python tools/check_source.py
```

## 测试与评测

原开发环境最近一次运行 `pytest`：567 项通过（2026-11-03）。部分测试依赖未公开的数据文件。

检索评测、业务回归及各自的数据范围见 [评测记录](docs/evaluation.md)。

## 已知问题

- 复杂表头、跨页表格和单位关联仍有未解决的问题；财务查询只开放已审核字段。
- 员工回答仍标记为草稿，需结合引用核对。
- 暂不支持指定年度的最高价、最低价查询。
- 会话只保存基本历史，尚未实现 LangGraph 节点级断点恢复。
"""
ARCHITECTURE = """# 架构

浏览器通过 FastAPI 提交问题。`DemoEngine` 根据问题类型选择文档问答、财务目录查询或行情查询。

## 员工问答

```text
问题 + 会话历史
    ↓
识别公司、报告期和主体范围
    ↓
BM25 + BGE 向量召回 → RRF 合并 → 任务/字段补召回
    ↓
Cross-Encoder 重排 → Top 5
    ↓
DeepSeek 生成 → 事实、年度、单位与引用检查
    ↓
回答草稿 + 原文摘录 + PDF 页码
```

检索代码在 [`retrieval/`](../src/app/retrieval/)，LangGraph 流程在
[`qa/workflow.py`](../src/app/qa/workflow.py)。公司或年份不明确时先要求补充，
缺少受支持事实时返回证据或拒答。

向量存储使用 Chroma，嵌入模型为 BGE，重排在 GPU 上运行。
索引与模型版本通过发布元数据和文件哈希校验。

## 财务与公告

[`qa/catalog.py`](../src/app/qa/catalog.py) 从审核目录查找字段，保留报告期、单位和来源页码。
金额换算、同比和差额由 `Decimal` 计算，这条路径不调用 LLM。

目前有 320 条财务比较事实和 83 条公告相关事实。2024 年财务值取自 2025 年报的比较列，
保留调整或重述口径。

## 行情

[`market/service.py`](../src/app/market/service.py) 负责行情意图解析、腾讯行情接口和快照回放。
响应带来源、业务时间与获取时间；手动回放显示为非实时数据。

暂不支持年度最高价、最低价，识别到这类请求后直接提示。相邻追问可保留公司和时间条件，
切换到文档问题后结束行情上下文。

## Web 与会话

[`web/api.py`](../src/app/web/api.py) 提供聊天和来源接口，前端位于 `web/static/`。SQLite
保存基本会话历史。

公网通过 [`web/public.py`](../src/app/web/public.py) 增加 HTTP Basic 认证、Host/Origin
检查和持久化请求额度。公网与本地使用不同的会话库，服务仍监听本机回环地址。
"""
REPRODUCTION = """# 运行说明

## 环境

- Python 3.11
- uv
- Windows + NVIDIA GPU（原开发环境）

Linux、CPU 环境尚未完成运行验证。

## 安装依赖

```powershell
uv sync --extra gpu --locked
Copy-Item .env.example .env
```

在 `.env` 中填写 `DEEPSEEK_API_KEY`。其余生成参数可参考空模板。

## 准备数据

仓库只包含代码和配置。启动完整服务还需要以下文件，当前未随仓库发布：

| 输入 | 用途 |
|---|---|
| 原始 PDF 与文档清单 | 来源展示、报告期和文件校验 |
| BGE 嵌入与重排模型 | 本地向量检索和重排 |
| 切片、Chroma 索引与发布元数据 | 恢复已发布的检索库 |
| 财务/公告目录及审核证据 | 字段查询、计算和引用检查 |

运行时会检查这些文件的版本和哈希；仅下载模型、安装依赖还不足以启动。
当前未提供从空目录重建全部输入的一键流程。

## 启动

在输入准备完整的原开发环境中运行：

```powershell
.\\Start-LocalDemo.ps1
```

默认地址为 `http://127.0.0.1:8000`。缺少数据或发布元数据时会启动失败。

## 检查与测试

只检查仓库文件哈希和 Python 语法，不需要模型、API Key 或第三方依赖：

```bash
python tools/check_source.py
```

安装依赖后可通过 `uv run pytest` 运行测试；其中依赖语料、索引和审核文件的用例需要先补齐输入。
历史测试结果见 [评测记录](evaluation.md)。
"""
EVALUATION = """# 评测记录

以下是原开发环境保存的结果，截至 2026-11-03。这里按检索、业务回归和代码测试分别记录。

## 检索

v4 评测集有 30 道可回答题和 5 道拒答题。检索取 Top 5，以指定来源页为目标：

| 指标 | 结果 |
|---|---:|
| 平均来源页 Recall@5 | 93.33% |
| 所需来源页全部命中 | 26/30 |

Recall 是各题来源页召回率的平均值，26/30 是找齐所有来源页的题数。5 道拒答题未计入该检索指标，
也未在这份报告中评分。来源页命中不能代表回答正确。

## 业务回归

已知业务用例 85/85 通过，覆盖财务、公告、员工查询、计算、边界与追问。用例及事实已在开发中见过，
由同一助手编制和审核，不属于独立答案准确率测试。

另有 403 项选定事实证据检查：320 条财务比较事实和 83 条公告相关事实。
这些检查没有覆盖全语料的表格语义。

## 行情约束

版本：`tencent-market-qfq-v1.3-constraint-routing`。

已知场景在本地 HTTP 和公网 HTTP 各通过 10/10，耗时分别为 2.13 秒和 3.89 秒，
计时包括初始化后的串行请求，不含启动。该检查未调用 LLM。

覆盖短年份、年度极值请求和相邻纠正追问。年度极值查询的预期行为是返回“不支持”，
尚未实现全年取数与极值计算。

## 代码测试

2026-11-03 在原开发环境运行完整 pytest：567 项通过。部分测试依赖本仓库没有附带的运行数据；
这不是公开仓库的 CI 结果。

指标摘要及原始报告 SHA256 保存在 [`review/metrics.json`](../review/metrics.json)。
原始评测数据和报告未公开，以上结果均来自本机实验。
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
    (target_dir / "docs/evaluation.md").write_text(EVALUATION, encoding="utf-8")
    shutil.copyfile(Path(__file__), target_dir / "scripts/prepare_github_portfolio.py")
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
