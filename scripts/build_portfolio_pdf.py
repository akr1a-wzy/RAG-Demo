"""Build the four-page project brief using ReportLab, without any API requests."""

import argparse
import json
import shutil
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
INK, MUTED, LINE, ACCENT = map(HexColor, ["#242426", "#77777c", "#e4e5e7", "#30705a"])
W, H = A4
M, BODY = 46, W - 92


class Brief:
    def __init__(self, path, font):
        pdfmetrics.registerFont(TTFont("CN", str(font)))
        self.c = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
        self.c.setTitle("有据 - 面向复杂年报的可追溯问答系统")
        self.c.setAuthor("有据项目")

    def text(self, x, y, value, size=10, color=INK, font="CN"):
        self.c.setFillColor(color)
        self.c.setFont(font, size)
        self.c.drawString(x, y, value)

    def paragraph(self, text, x, y, width=BODY, size=10, leading=18, color=MUTED):
        for paragraph in text.split("\n"):
            line = ""
            for char in paragraph:
                if pdfmetrics.stringWidth(line + char, "CN", size) > width and line:
                    self.text(x, y, line, size, color)
                    y -= leading
                    line = ""
                line += char
            if line:
                self.text(x, y, line, size, color)
                y -= leading
        if y < 48:
            raise ValueError("PDF text would overlap the footer")
        return y

    def line(self, y, x=M, width=BODY):
        self.c.setStrokeColor(LINE)
        self.c.setLineWidth(.6)
        self.c.line(x, y, x + width, y)

    def box(self, x, top, width, height, fill="#f7f7f8"):
        self.c.setFillColor(HexColor(fill))
        self.c.setStrokeColor(LINE)
        self.c.roundRect(x, top - height, width, height, 7, fill=1, stroke=1)

    def page(self, number, label):
        if number > 1:
            self.c.showPage()
        self.text(M, H - 36, "有据 / 项目说明", 9, MUTED)
        self.text(W - M - 77, H - 36, f"{number:02d} / 04", 9, MUTED, "Helvetica")
        self.line(H - 48)
        self.text(M, 28, "2026-11-03 · 材料快照 · 本机项目证据", 8, MUTED)
        self.text(W - M - 83, 28, label, 8, MUTED)

    def heading(self, title, subtitle):
        self.text(M, 743, title, 23)
        self.paragraph(subtitle, M, 712, size=10, leading=18)

    def lane(self, y, title, steps):
        self.text(M, y, title, 11, ACCENT)
        width = (BODY - 24) / 4
        for i, (name, detail) in enumerate(steps):
            x = M + i * (width + 8)
            self.box(x, y - 16, width, 62)
            self.text(x + 10, y - 38, name, 9)
            self.paragraph(detail, x + 10, y - 55, width - 20, 7.5, 12)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/simhei.ttf"))
    args = parser.parse_args()
    # Assert the reported result, without rerunning or inspecting sealed questions.
    retrieval = json.loads((ROOT / "eval/results/blind_v4_once.json").read_text(encoding="utf-8"))
    summary = retrieval["summary"]
    business = json.loads((ROOT / "eval/results/development/catalog-demo-v3/report.json")
                          .read_text(encoding="utf-8"))
    assert summary["answerable_count"] == 30 and summary["all_sources_hit_count"] == 26
    assert round(summary["mean_source_page_recall_at_5"] * 100, 2) == 93.33
    assert (business["passed"], business["denominator"]) == (85, 85)
    latest = json.loads((ROOT / "docs/retrospective/evidence/phase28-market-fix-final.json")
                       .read_text(encoding="utf-8"))
    test_count = latest["tests"]["passed"]
    assert test_count == 567 and latest["version"] == "tencent-market-qfq-v1.3-constraint-routing"
    config = json.loads((ROOT / "tmp/public-demo/config.json").read_text(encoding="utf-8"))
    origin = config["origin"]
    output = ROOT / "output/pdf/youju-project.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    brief = Brief(output, args.font)
    b = brief

    b.page(1, "项目与架构")
    b.text(M, 753, "RAG / AI APPLICATION ENGINEERING", 9, ACCENT, "Helvetica")
    b.text(M, 704, "有据", 34)
    b.text(M, 666, "面向复杂年报的可追溯问答系统", 20)
    b.paragraph("将公开年报与公告中的问题，拆成能检索、能核对、能追溯的事实。"
                "重点处理年份、单位、主体与来源；通过失败记录和分层评测约束系统边界。",
                M, 632, leading=18)
    b.line(573)
    stats = [("20", "家上市公司"), ("60", "份年报与公告"), ("5,364", "页PDF"),
             ("32,668", "个向量子片段")]
    for i, (number, label) in enumerate(stats):
        x = M + i * BODY / 4
        b.text(x, 540, number, 24, INK, "Helvetica")
        b.text(x, 519, label, 9, MUTED)
    b.line(500)
    b.lane(469, "员工问答：检索生成，输出保留草稿标记", [
        ("请求恢复", "公司、年份与主体"), ("混合检索", "BM25 + 向量 + RRF"),
        ("GPU重排", "20候选，Top5"), ("生成与校验", "DeepSeek，引用绑定")])
    b.lane(357, "财务与公告：核准字段直接列值或计算", [
        ("受限意图解析", "字段、期间与操作"), ("审核字段目录", "坐标与PDF哈希"),
        ("列值与计算", "Decimal及口径约束"), ("返回原文", "年度、单位、物理页")])
    b.paragraph("行情通过独立工具查询价格、估值及前复权日K，展示业务时间与获取时间。"
                "快照回放由用户显式选择，并标注非实时。", M, 246, size=9.5, leading=17)
    b.text(M, 174, "实现与交付", 12)
    b.paragraph("Python / PyMuPDF / pypdf / BGE / Chroma / LangGraph / DeepSeek / "
                "FastAPI / SQLite\n"
                "提供浏览器Demo、原文PDF跳转、会话恢复、审核字段覆盖和可核验的实验记录。",
                M, 151, size=9, leading=17)

    b.page(2, "体验与来源")
    b.heading("从一次提问，走到一页原文。", "以下为已保存的核准财务回答示例，非实时生成。"
              "原始数据、原单位和来源页一并保留。")
    b.box(M, 657, BODY, 67)
    b.text(M + 16, 635, "问题", 9, MUTED)
    b.text(M + 16, 609, "宁德时代2025年营业收入是多少？", 15)
    b.text(M, 553, "原文事实 / 核准字段直接列值", 10, ACCENT)
    b.text(M, 512, "423,701,834", 32, INK, "Helvetica")
    b.text(M + 222, 516, "千元", 13)
    b.paragraph("公司：宁德时代（300750）  报告期：2025\n"
                "来源：《2025年年度报告》，PDF物理第11页。", M, 483, size=10, leading=19)
    b.box(M, 418, BODY, 85)
    b.text(M + 16, 395, "证据定位", 10)
    b.paragraph("指标行：营业收入；年度列：2025；原值：423,701,834；单位：千元。\n"
                "引用原文包含相邻年度值，最终字段须与对应年度列和行位置绑定，不能仅凭数字出现。",
                M + 16, 374, BODY - 32, 9, 16)
    b.text(M, 299, "体验路径", 13)
    b.paragraph("1. 在项目页点击财务示例，确认问题后发送。\n"
                "2. 点击回答引用，展开原文证据面板。\n"
                "3. 打开原始PDF，核对物理第11页、年度列和单位。\n"
                "4. 可继续追问总资产；已有会话可刷新恢复。", M, 273, leading=21)
    b.line(170)
    b.paragraph("另可体验：员工人数；最新价格、PE、PB与市值。员工生成保持草稿，行情注明业务时间。"
                "年度最高/最低价尚未实现，会明确提示不支持，不使用最新报价代替。",
                M, 146, size=9, leading=17)
    b.text(M, 88, "访问需要演示账号与另行提供的访问码，材料内不包含凭据。", 8.5, MUTED)

    b.page(3, "难点与取舍")
    b.heading("把失败，变成可核查的改进。", "实现重点是事实约束、评测角色和可见的故障行为。"
              "以下均有代码、测试或阶段记录支撑。")
    cases = [
        ("A / 数字出现，不等于事实成立。",
         "现象：同页存在多个年度和单位，仅验证数字出现会放过错列回答。",
         "处理：将指标、年度列、数值、局部单位与主体绑定；对选定财务事实保存PDF哈希和坐标证据。",
         "取舍：合理但未支持的复杂表头可能被拦截，全语料表格语义仍未逐格验收。"),
        ("B / 失败题进入回归，原始成绩不改写。",
         "现象：一次验收失败后继续围绕同一题集修复，会引入已见题偏差。",
         "处理：冻结失败响应、题目与版本；旧题正式转固定回归，新版本另建验收，保留原报告。",
         "取舍：已知业务回归85/85只能说明该范围回归通过，不能写成新问题准确率100%。"),
        ("C / 年度极值，不能用最新报价替代。",
         "现象：25年最高股价误返最新报价；纠正追问“问的是最高”又误入文档检索。",
         "处理：规范短年度、保留极值与相邻纠正条件；未支持的年度行情在取数前明确阻断。",
         "取舍：只修复意图与范围保护，未实现全年取数或极值计算；不把拒答改进包装成功能新增。"),
    ]
    y = 652
    for title, problem, action, tradeoff in cases:
        b.text(M, y, title, 14)
        y = b.paragraph(problem, M, y - 27, size=10, leading=18)
        y = b.paragraph(action, M, y - 3, size=10, leading=18)
        y = b.paragraph(tradeoff, M, y - 3, size=9, leading=17, color=ACCENT)
        b.line(y - 8)
        y -= 43
    b.paragraph("源码阅读重点：app/qa/facts.py、app/qa/catalog.py、app/qa/workflow.py、"
                "app/retrieval/published.py、app/web/public.py。完整路径以源码包清单为准。",
                M, 106, size=8.5, leading=15)

    b.page(4, "评测与边界")
    b.heading("不同分母，回答不同的问题。", "材料来自本机历史报告。检索召回、业务回归、证据检查"
              "和代码测试分别报告，不混作总体答案准确率。")
    rows = [
        ("93.33%", "指定来源页 Recall@5", "v4一次检索验收，30道可回答题；不是答案准确率。"),
        ("26/30", "完整来源找齐", "与平均Recall定义不同；另外5道拒答未在该检索阶段评分。"),
        ("85/85", "已知业务回归", "财务、公告、员工、计算、边界与追问；同一助手审核。"),
        ("403项", "选定事实证据检查", "320条财务比较事实 + 83条公告相关事实，非全表语义验收。"),
        (f"{test_count}项", "最新完整代码测试",
         "2026-11-03通过，含2项既有依赖警告，不是问答准确率。"),
    ]
    y = 650
    for value, title, detail in rows:
        b.text(M, y, value, 18, ACCENT, "CN" if "项" in value else "Helvetica")
        b.text(M + 100, y + 2, title, 10)
        b.paragraph(detail, M + 100, y - 17, BODY - 100, 8.2, 13)
        b.line(y - 36)
        y -= 66
    b.paragraph("本机54场景一次验收：文档回答29/30、严格拒答8/8；24个事实全已见，同一助手"
                "编题与审核，非事实或人员隔离盲测。后续85题回归与其契约、分母不同，不能直接作提升比较。",
                M, 302, size=8.5, leading=15)
    b.text(M, 232, "当前限制", 12)
    b.paragraph("复杂表头风险保留；2024值来自2025年报比较列。员工生成仍为草稿；年度最高/最低价"
                "与未来预测未支持。本机、网络和隧道须持续运行，未宣称生产SLA。",
                M, 210, size=9, leading=17)
    b.text(M, 130, "项目入口：点击打开在线项目说明", 10, ACCENT)
    b.c.linkURL(origin + "/static/project.html", (M, 126, M + 280, 144))
    b.paragraph("页面提供4页说明与源码阅读包。源码包含实现、测试、依赖与说明；不含密钥、会话、"
                "PDF、模型、冻结索引或完整审核证据，完整运行需另行准备。",
                M, 107, size=8.5, leading=15)
    b.c.save()
    published = ROOT / "src/app/web/static/downloads/youju-project.pdf"
    published.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(output, published)
    print(json.dumps({"pdf": str(output), "published": str(published), "pages": 4,
                      "bytes": output.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
