# 运行说明

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
.\Start-LocalDemo.ps1
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
