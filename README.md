# 有据 · RAG-Demo

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
