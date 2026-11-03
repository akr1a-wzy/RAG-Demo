# 架构

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
