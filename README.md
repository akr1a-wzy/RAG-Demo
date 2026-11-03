# RAG-Demo

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

原开发环境完整测试记录：607 项通过（2026-11-03）。部分测试依赖未公开的数据文件。

检索评测、业务回归及各自的数据范围见 [评测记录](docs/evaluation.md)。

## 已知问题

- 复杂表头、跨页表格和单位关联仍有未解决的问题；财务查询只开放已审核字段。
- 员工回答仍标记为草稿，需结合引用核对。
- 暂不支持指定年度的最高价、最低价查询。
- 会话只保存基本历史，尚未实现 LangGraph 节点级断点恢复。
