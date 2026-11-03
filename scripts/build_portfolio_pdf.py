"""Build the four-page project note; no model, market or chat requests."""

import argparse
import json
import shutil
from pathlib import Path

from reportlab.lib import textsplit
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as paragraph_module

ROOT = Path(__file__).resolve().parents[1]
W, H = A4
M, BODY = 50, W - 100
INK, MUTED, RULE = map(HexColor, ["#222222", "#626262", "#c8c8c8"])
GITHUB = "https://github.com/akr1a-wzy/RAG-Demo"
# ReportLab's default kinsoku list omits several Chinese full-width punctuation marks.
textsplit.ALL_CANNOT_START += "，；：？！）》”’"
paragraph_module.ALL_CANNOT_START = textsplit.ALL_CANNOT_START


class Note:
    def __init__(self, path, body_font, heading_font):
        pdfmetrics.registerFont(TTFont("BodyCN", str(body_font), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("HeadCN", str(heading_font), subfontIndex=0))
        self.c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
        self.c.setTitle("有据 | A股年报问答项目说明")
        self.c.setAuthor("RAG-Demo")
        self.y = 0

    def text(self, x, y, value, size=10, font="BodyCN", color=INK):
        self.c.setFillColor(color)
        self.c.setFont(font, size)
        self.c.drawString(x, y, value)

    def line(self, y, width=BODY):
        self.c.setStrokeColor(RULE)
        self.c.setLineWidth(.45)
        self.c.line(M, y, M + width, y)

    def page(self, number, title, subtitle):
        if number > 1:
            self.c.showPage()
        self.text(M, H - 34, "有据 / RAG-Demo", 8.5, "HeadCN", MUTED)
        self.c.setFillColor(MUTED)
        self.c.setFont("Helvetica", 8.5)
        self.c.drawRightString(W - M, H - 34, f"{number} / 4")
        self.line(H - 46)
        self.text(M, H - 88, title, 22, "HeadCN")
        self.text(M, H - 113, subtitle, 9, "BodyCN", MUTED)
        self.line(43)
        self.text(M, 28, "项目说明 · 2026年11月", 8, "BodyCN", MUTED)
        self.y = H - 139

    def para(self, text, size=10.5, leading=19, color=INK, gap=10,
             x=M, width=BODY, font="BodyCN"):
        style = ParagraphStyle(
            "body", fontName=font, fontSize=size, leading=leading,
            textColor=color, alignment=TA_LEFT, wordWrap="CJK",
            splitLongWords=True, allowWidows=0, allowOrphans=0,
        )
        paragraph = Paragraph(text, style)
        _, height = paragraph.wrap(width, H)
        if self.y - height < 61:
            raise ValueError(f"Content overlaps footer: {text[:40]}")
        paragraph.drawOn(self.c, x, self.y - height)
        self.y -= height + gap

    def heading(self, text):
        self.y -= 9
        self.para(text, size=12, leading=18, font="HeadCN", gap=8)

    def small(self, text, gap=9):
        self.para(text, size=9, leading=16, color=MUTED, gap=gap)

    def link(self, label, url):
        self.para(f'{label}：<link href="{url}" color="#304f6b">{url}</link>',
                  size=9, leading=16, gap=6)

    def table(self, rows, widths):
        """Plain three-rule table with wrapped cells."""
        style = ParagraphStyle("cell", fontName="BodyCN", fontSize=9.2,
                               leading=15, wordWrap="CJK", textColor=INK)
        self.line(self.y)
        for index, row in enumerate(rows):
            cells = [Paragraph(value, style) for value in row]
            heights = [cell.wrap(width - 15, H)[1]
                       for cell, width in zip(cells, widths, strict=True)]
            height = max(heights) + 16
            if self.y - height < 61:
                raise ValueError("Table overlaps footer")
            x = M
            for cell, width, cell_height in zip(cells, widths, heights, strict=True):
                cell.drawOn(self.c, x + 4, self.y - 8 - cell_height)
                x += width
            self.y -= height
            if index == 0:
                self.line(self.y)
        self.line(self.y)
        self.y -= 13


def check_results():
    retrieval = json.loads((ROOT / "eval/results/blind_v4_once.json").read_text("utf-8"))
    summary = retrieval["summary"]
    assert summary["answerable_count"] == 30 and summary["all_sources_hit_count"] == 26
    assert round(summary["mean_source_page_recall_at_5"] * 100, 2) == 93.33
    business = json.loads((ROOT / "eval/results/development/catalog-demo-v3/report.json")
                          .read_text("utf-8"))
    assert (business["passed"], business["denominator"]) == (85, 85)
    latest = json.loads((ROOT / "docs/retrospective/evidence/phase28-market-fix-final.json")
                       .read_text("utf-8"))
    assert latest["tests"]["passed"] == 567
    # Read saved summaries only; do not rerun sealed evaluations to typeset a document.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/simsun.ttc"))
    parser.add_argument("--heading-font", type=Path,
                        default=Path("C:/Windows/Fonts/msyh.ttc"))
    args = parser.parse_args()
    check_results()
    config = json.loads((ROOT / "tmp/public-demo/config.json").read_text("utf-8"))
    origin = config["origin"]
    output = ROOT / "output/pdf/youju-project.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    b = Note(output, args.font, args.heading_font)

    b.page(1, "有据：A股年报问答", "项目用途与使用示例")
    b.para("项目用公开年报和公告回答上市公司相关问题。用户在网页输入问题，"
           "回答中附上原文摘录和 PDF 页码，可以继续打开原文件核对。"
           "目前整理了 20 家公司的 60 份文档，共 5,364 页。")
    b.para("实现分为三部分：员工人数走检索与生成，财务和公告从审核目录读取字段，"
           "股价与 K 线交给独立行情接口。三类问题共用一个聊天页面，支持追问和历史会话。")
    b.heading("可以查询什么")
    b.table([
        ("类型", "示例问题", "处理方式"),
        ("员工", "中国神华2025年在职员工数量合计是多少人？", "检索年报后生成草稿，附引用"),
        ("财务", "宁德时代2025年营业收入是多少？", "目录取值；代码计算同比与换算"),
        ("行情", "贵州茅台最新股价、PE、PB和市值是多少？", "查询行情源，显示数据时间"),
    ], [48, 278, BODY - 326])
    b.heading("一个保存的回答示例")
    b.para("问题：宁德时代2025年营业收入是多少？<br/>"
           "回答：423,701,834 千元。<br/>"
           "来源：《2025年年度报告》，PDF 物理第 11 页。", gap=8)
    b.para("这条回答直接读取审核目录，没有调用生成模型。字段同时记录指标行、年度列、"
           "原值和单位。点击引用可以查看原文，再跳转到 PDF 对应页。", gap=8)
    b.small("示例为已保存的字段结果，金额沿用报告原单位。")
    b.heading("在线体验")
    b.link("Demo", origin + "/")
    b.link("代码", GITHUB)
    b.small("Demo 需要另行提供的访问码。本机和 ngrok 隧道保持运行时才能访问。")

    b.page(2, "实现流程", "检索、字段查询与行情分别处理")
    b.heading("员工问答")
    b.para("先从当前问题和历史消息中识别公司、报告期和主体，再执行 BM25 与 BGE 向量召回，"
           "用 RRF 合并结果。针对员工合计等字段补充检索后，交给 Cross-Encoder 重排，"
           "从每个检索任务的 20 个候选中保留 Top 5。")
    b.para("DeepSeek 根据检索结果生成回答，后续检查所引用的事实、年份、单位和来源。"
           "员工回答仍标为草稿；公司或期间不清楚时先询问，缺少受支持事实时展示证据或拒答。")
    b.small("流程：问题与历史 → 公司/期间识别 → 混合检索 → 重排 → 生成 → 引用与事实检查")
    b.heading("财务与公告")
    b.para("财务和公告问题使用单独的字段目录。目前收录 320 条财务比较事实和 83 条公告相关事实。"
           "目录中的记录与原 PDF、坐标和审核证据关联，查询时读取数值，使用 Decimal 计算换算、"
           "同比和差额。")
    b.para("这部分不调用 LLM。2024 年数值来自 2025 年报的比较列，调整或重述口径随字段保留。"
           "目录外的指标和未支持的限定条件不能按普通字段查询。")
    b.heading("行情与网页")
    b.para("行情接口提供最新报价和前复权日 K 线，返回来源、业务时间与获取时间。"
           "快照只在用户手动选择回放时使用，并标为非实时。网页由 FastAPI 提供，"
           "SQLite 保存基本会话历史，公网入口增加认证、同源检查和持久化请求额度。")
    b.heading("代码位置")
    b.table([
        ("模块", "主要内容"),
        ("src/app/retrieval/", "PDF 解析、切分、BGE 向量、Chroma、混合检索与重排"),
        ("src/app/qa/", "LangGraph 问答流程、字段目录、计算、事实和引用检查"),
        ("src/app/market/", "行情意图、接口、日 K 线与快照"),
        ("src/app/web/", "API、会话存储、认证与前端静态文件"),
    ], [146, BODY - 146])

    b.page(3, "开发中遇到的问题", "三个影响实际回答的例子")
    b.heading("1. 同一张表里，数值对应了错误年度")
    b.para("年报表格常把两个年度并排列出，单位也可能写在表格上方。早期只核对数字是否出现在"
           "原文中，无法拦住取错列或套错单位的回答。")
    b.para("后续把指标行、年度列、数值、局部单位和主体一起绑定。对选定财务字段保存 PDF 哈希、"
           "坐标和审核图，再从审核目录返回结果。简单的显式年度列已有检查，复杂跨页表头仍有"
           "未解决的关联问题，因此没有把全部财务表格开放给生成模型。")
    b.small("相关代码：src/app/qa/facts.py、src/app/qa/catalog.py")
    b.heading("2. 页码命中后，仍然缺少回答所需的事实")
    b.para("检索命中来源页，不代表返回的片段已经包含所需字段。同一页可能既有员工合计，"
           "也有专业构成或不同公司主体。仅按整页命中评分，会漏掉这类问题。")
    b.para("检索增加了任务和字段补召回；问答阶段继续检查主体、字段和支持事实。"
           "评测中分别记录来源页召回和全部来源找齐的题数。已经用于修复的失败题保留为回归，"
           "原一次验收结果不回写，避免把反复调试同一批题后的结果当成新问题表现。")
    b.small("相关代码：src/app/retrieval/query_tasks.py、src/app/qa/selection.py")
    b.heading("3. 问年度最高价，却返回最新报价")
    b.para("实际提问“宁德时代25年股价最高是多少”曾被当成最新股价查询；继续问“问的是最高”，"
           "又转入了无关的文档检索。原因是行情意图丢失了短年份和最高价条件，纠正追问也没有"
           "保留原来的查询范围。")
    b.para("行情分支现在保留短年份、极值条件和相邻纠正追问，并在取数前检查支持范围。"
           "全年行情取数与极值计算尚未实现，所以这类问题目前明确提示不支持。"
           "该修复只覆盖行情分支，财务短年份解析的问题仍待处理。")
    b.small("相关代码：src/app/market/service.py；测试：tests/unit/test_market_constraints.py")

    b.page(4, "测试与运行", "结果来自本地开发环境，记录截至2026年11月3日")
    b.table([
        ("检查", "结果", "范围"),
        ("指定来源页 Recall@5", "93.33%", "v4 的 30 道可回答题，取各题召回率平均值"),
        ("全部来源页找齐", "26/30", "同一批检索题；另外 5 道拒答未在该阶段评分"),
        ("已知业务回归", "85/85", "财务、公告、员工、计算、边界和追问"),
        ("完整 pytest", "567 项通过", "2026-11-03 原开发环境；部分测试依赖本地数据"),
    ], [146, 86, BODY - 232])
    b.small("Recall 不是答案准确率。85 道业务用例及事实已在开发中见过，由同一助手编制和审核。"
            "另有 403 项选定事实证据检查，没有覆盖全语料表格语义。原报告摘要与 SHA256 在"
            "仓库 review/metrics.json，原始评测数据与报告未随仓库公开。", gap=8)
    b.heading("尚未解决的问题")
    b.para("复杂表头、跨页表格和单位关联仍需继续处理；员工生成回答需要结合引用核对。"
           "财务查询中的“总营收”和“25年”等表达目前可能被拒绝，已定位到别名和年份解析，"
           "尚未修复。年度最高价、最低价，以及 LangGraph 节点级断点恢复也尚未实现。")
    b.heading("本地运行条件")
    b.para("原环境为 Windows、Python 3.11 和 NVIDIA GPU，依赖由 uv.lock 固定。"
           "在输入准备完整的环境中安装依赖并启动：", gap=6)
    b.para("uv sync --extra gpu --locked<br/>.\\Start-LocalDemo.ps1",
           size=9, leading=16, font="Courier", gap=8)
    b.small("本地默认地址：http://127.0.0.1:8000", gap=8)
    b.para("公开源码不含密钥、会话、原始 PDF、模型、索引和审核目录。完整服务依赖这些文件，"
           "克隆后还不能直接启动。仓库 tools/check_source.py 可以独立检查文件哈希和 Python 语法；"
           "数据准备情况见 docs/reproduction.md。", gap=8)
    b.heading("入口与材料")
    b.link("GitHub", GITHUB)
    b.link("项目页", origin + "/static/project.html")
    b.small("项目页提供 Demo 和源码阅读包；访问码另行提供，PDF 中不包含凭据。")

    b.c.save()
    published = ROOT / "src/app/web/static/downloads/youju-project.pdf"
    published.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(output, published)
    print(json.dumps({"pdf": str(output), "published": str(published), "pages": 4,
                      "bytes": output.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
