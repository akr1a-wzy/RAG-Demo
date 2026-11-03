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

## 检查与测试

只检查仓库文件哈希和 Python 语法，不需要模型、API Key 或第三方依赖：

```bash
python tools/check_source.py
```

安装依赖后可通过 `uv run pytest` 运行测试；其中依赖语料、索引和审核文件的用例需要先补齐输入。
历史测试结果见 [评测记录](evaluation.md)。
