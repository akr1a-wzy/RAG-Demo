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

Demo 当前无需登录，由本机通过 ngrok 提供服务，离线时无法访问。
所有访客共享每日 50 次问答尝试（北京时间零点刷新）和连续 60 秒内 20 次 API 写请求额度。
业务处理后的拒答或失败仍计额度；超限需稍后或次日再试。详见[访问限额](docs/reproduction.md#访问限额)。

## 功能

- **年报问答**：目前主要支持员工人数查询。BM25 和 BGE 向量混合检索，经重排后交给 DeepSeek
  生成回答，再检查事实、年度、单位和引用。
- **财务与公告查询**：从审核过的字段目录读取数值，用 `Decimal` 计算同比、差额和单位换算。
- **行情查询**：最新报价、PE、PB、市值、前复权日 K 线；支持手动回放保存的行情快照。
- **Web 界面**：连续对话、历史会话、PDF 跳转、浅色与深色主题。
- **查询理解**：财务与行情共用年份、别名和追问解析；未知条件保留并校验。

当前数据覆盖 20 家公司、60 份年报及公告。财务和公告查询使用结构化目录，员工问答走 RAG，两条路径在
[架构说明](docs/architecture.md) 中分别介绍。
三个具体开发案例见[开发问题与处理](docs/development.md)：召回修复引入关联财务公司错排、年度单位错配、查询条件丢失。

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

原开发环境完整测试记录：{test_count} 项通过（2026-11-03）。部分测试依赖未公开的数据文件。

检索评测、业务回归及各自的数据范围见 [评测记录](docs/evaluation.md)。

## 已知问题

- 复杂表头、跨页表格和单位关联仍有未解决的问题；财务查询只开放已审核字段。
- 员工回答仍标记为草稿，需结合引用核对。
- 暂不支持指定年度的最高价、最低价查询。
- 会话只保存基本历史，尚未实现 LangGraph 节点级断点恢复。
"""
ARCHITECTURE = """# 架构

浏览器通过 FastAPI 提交问题。`qa/query_understanding.py`先规范化年份和别名、恢复相邻追问、
检查能力范围，再由`DemoEngine`选择文档问答、财务目录查询或行情查询。原问题和未知条件保留。
“总营收”按营业收入口径列示；营业总收入是另一个未核准指标。年度最高/最低行情仍不支持。

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

公网通过 [`web/public.py`](../src/app/web/public.py) 提供可选 HTTP Basic 认证、Host/Origin
检查和持久化请求额度。当前在线 Demo 关闭登录验证，额度仍保留，所有访客共享。
公网与本地使用不同的会话库，服务仍监听本机回环地址。
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

## 公网访问模式

完整运行输入和 ngrok 账号配置就绪后，可运行 `Start-PublicDemo.ps1` 发布公网入口。
新建配置默认要求账号和访问码；当前在线 Demo 已显式关闭登录。切换示例：

```powershell
python scripts/run_public_demo.py prepare --origin https://YOUR-NGROK-DOMAIN --no-auth
```

然后重启公网服务使其生效。使用 `--auth` 可恢复认证；普通启动会保留已有配置的模式。
访问码、ngrok 凭据和配置仅保存在本机，不随仓库提供。
两种模式均保留同源检查与下列额度。

### 访问限额

- 所有访客共享每天 50 次问答尝试，北京时间每日零点换日，不是每人 50 次或 50 次模型生成。
- 任意连续 60 秒内最多 20 次 API 写请求，覆盖 `/api/*` 的 POST、PUT、PATCH、DELETE，
  包括创建或清空会话；每日问答额度仅计 `POST /api/chat`（含尾斜杠）。
- 入口校验通过、问答进入业务处理前计数，后续拒答或失败不退额度。入口校验拦截和已超限请求不计数。
- 额度保存在 SQLite 中，重启不会清零。超限返回 HTTP 429，按提示稍后或次日重试。
- 浏览页面、查看来源和下载 PDF、源码 ZIP 的 GET 请求不计上述额度。

实现见 [`web/public.py`](../src/app/web/public.py)，回归见
[`test_public_entry.py`](../tests/unit/test_public_entry.py)。

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

## 查询理解与行情约束

版本：`query-understanding-v1`，行情 `tencent-market-qfq-v1.4-query-understanding`。

统一查询理解后的已知场景，本机 HTTP 16/16（2.05 秒）、公网 HTTP 6/6（2.82 秒）。
计时包括初始化后串行请求、行情提供方 IO 和保全检查，不含重启和模型加载；0 次生成，
两处共 2 次实际行情取数。这是已知开发回归，不是独立答案准确率。

覆盖总营收别名、短年份、相邻追问、未知限定和能力前置检查。总营收按营业收入口径明示，
营业总收入仍是独立未核准指标。年度极值预期为“不支持”，尚未实现全年取数与极值计算。

## 免登录入口

免登录切换后的匿名 HTTPS 检查 23/23（3.6655 秒），包含 2 次核准财务问答、0 次生成；
该轮还核对了重启前后预算与原会话保全。这是访问与已知业务检查，不是新答案验收。

## 代码测试

2026-11-03 在原开发环境运行完整 pytest：{test_count} 项通过。部分测试依赖本仓库没有附带的运行数据；
这不是公开仓库的 CI 结果。

指标摘要及原始报告 SHA256 保存在 [`review/metrics.json`](../review/metrics.json)。
原始评测数据和报告未公开，以上结果均来自本机实验。
"""

DEVELOPMENT = """# 开发问题与处理

记录截至 2026-11-03。以下来自真实开发记录；合成反例单独标明。源码与选定报告摘要已公开，
原始 PDF、失败响应和评测报告未随仓库提供。检索命中不能代替答案正确性。

## 1. 补回营收来源页，却把关联财务公司表排在首位

中国神华营收题的正确来源为物理第 8 页。最初该页不在 20 个候选中，扩大到 50 个后位于候选第 30，
仍未进入最终 Top 5。精排无法补回没收到的候选，扩大候选也不保证选对证据。

摘要表补召回初版让第 8 页进入 Top 5，却把第 124 页关联财务公司（控股股东的子公司）
收入 4,251 百万元排在首位；
上市主体收入应为 294,916 百万元。此时指定页 Recall 已为 1，但表内主体错误。当时尚未调用生成模型，
不能说系统已经生成了错误营收答案。

根因是任意子串“主要财务指标”匹配。改为独立标题边界，排除财务公司子表、季度表和正文交叉引用后，
正确第 8 页升至首位。该题保留为已知回归，不据此宣称全部主体识别通过；文档所属公司不等于表内主体。
实现见 [`retrieval/query_tasks.py`](../src/app/retrieval/query_tasks.py)。

## 2. 数字在原文中，年度与单位仍可能错配

早期事实检查只验证数字是否出现。合成反例中 2025 年为 200、2024 年为 100，
草稿写“2025 年为 100”仍可能通过。真实解析中也发现前表“万元”污染局部“元”的问题。

处理时绑定指标行、年度列、数值、局部单位与主体；选定财务字段另存 PDF 哈希、坐标和审核图，
使用审核目录直接返回值并由 Decimal 计算。代价是只开放已经审核的字段，不能承诺任意财务表格可答。

固定绑定回归 33/33，包含 30 个合成用例和 3 个真实来源重建陈述。后续 403 项证据检查仅覆盖选定事实，
复杂合并、跨页与重述表头仍有未验证风险。实现见 [`qa/facts.py`](../src/app/qa/facts.py)、
[`qa/catalog.py`](../src/app/qa/catalog.py)。

## 3. 不同路线各自解析，导致条件丢失

“宁德时代25年股价最高是多少”曾被当成最新价，追问“问的是最高”又进入文档检索；
财务“总营收”和“25年”则因未消除的别名、年份残留被拒。各路线独立解析，局部补词难以保持一致。

新增共享查询理解对象，记录原问题、规范化、领域、公司、年份、指标、操作、未知限定、继承与能力判定。
财务和行情共用规则，相邻追问只在受限范围继承，跨领域时清除旧操作，取数前拒绝不支持的请求。
“总营收”按营业收入口径明示；“营业总收入”保持独立且未核准。

采用确定性解析，无新增理解模型调用，容易追踪转换；代价是覆盖有限，未知表达可能保守拒答。
本机 16/16、公网 6/6 已知 HTTP 回归通过，0 次生成，共 2 次行情取数。年度最高/最低价仍未实现，
修复后的行为是明确提示不支持。实现见
[`qa/query_understanding.py`](../src/app/qa/query_understanding.py)。

检索、代码测试与业务回归的口径见[评测记录](evaluation.md)，访问方式与共享额度见[运行说明](reproduction.md)。
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
        test_count = json.loads(archive.read("review/metrics.json"))["code_tests"]["passed"]
        for name in archive.namelist():
            target = (target_dir / name).resolve()
            if not target.is_relative_to(target_dir):
                raise ValueError("Unsafe archive member")
            if name == "SOURCE_MANIFEST.json":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    (target_dir / "README.md").write_text(
        README.replace("{test_count}", str(test_count)), encoding="utf-8")
    (target_dir / "docs").mkdir()
    (target_dir / "docs/architecture.md").write_text(ARCHITECTURE, encoding="utf-8")
    (target_dir / "docs/reproduction.md").write_text(REPRODUCTION, encoding="utf-8")
    (target_dir / "docs/evaluation.md").write_text(
        EVALUATION.replace("{test_count}", str(test_count)), encoding="utf-8")
    (target_dir / "docs/development.md").write_text(DEVELOPMENT, encoding="utf-8")
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
