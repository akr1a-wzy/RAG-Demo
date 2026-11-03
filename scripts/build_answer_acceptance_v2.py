"""冻结新版问法/组合验收输入；沿用并复核PDF金标准，不调用待测QA或生成模型。"""

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from time import perf_counter

import pymupdf
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cases():
    result = []

    def add(key, group, question, codes=(), *, depends=None, evidence=False, mode="draft",
            metric="headcount_total"):
        record = {"id": "answer-v2-" + key, "group": group, "question": question,
                  "expected_mode": mode,
                  "fact_ids": [f"{code}:2025:{metric}" for code in codes],
                  "novelty": "known_regression" if group == "known_staff_regression"
                  else "new_wording_or_composition_existing_corpus_facts"}
        if depends:
            record["depends_on"] = "answer-v2-" + depends
        if evidence:
            record["evidence_only"] = True
        result.append(record)

    singles = [
        ("000858", "想核对五粮液2025年报里的员工人数合计，请给出人数和引用。"),
        ("000568", "泸州老窖在2025年报告期末，员工数量合计为多少人？附原文。"),
        ("600036", "请查招商银行2025年末员工人数，采用包含派遣人员的口径并给引用。"),
        ("600028", "按中国石化2025年报，本公司员工人数列示为多少名？引用原句。"),
        ("600030", "中信证券在2025年末披露的集团员工人数是多少？请给原文出处。"),
        ("000333", "2025年报中，美的集团员工数量合计一栏列了多少人？"),
        ("300750", "请找宁德时代2025年报，回答报告期末的员工人数合计并引用。"),
        ("601318", "2025年末平安体系内的员工人数为多少？只给年报原数和出处。"),
        ("600900", "请照长江电力2025年报原文列示，给出员工人数合计，不自行重算。"),
        ("601857", "中国石油截至2025年12月31日的员工人数合计是什么数值？附引用。"),
        ("000651", "格力电器2025年年报中的员工数量合计有多少人？请定位原文。"),
        ("600276", "帮我核实恒瑞医药2025年报的员工人数合计，给数字与引用。"),
    ]
    for code, question in singles:
        add("single-" + code, "new_staff_single", question, [code])
    comparisons = [
        ("dairy-solar", ["600887", "601012"],
         "2025年，伊利股份和隆基绿能各有多少在职员工？分公司列员工人数合计和原文。"),
        ("telecom-security", ["600050", "002415"],
         "请列中国联通、海康威视在2025年末各自的员工数量合计，两家公司各给一条引用。"),
        ("steel-pharma", ["600019", "600276"],
         "对照宝钢股份与恒瑞医药2025年报：员工人数合计各是多少？只列原文数字。"),
        ("bank-broker", ["600036", "600030"],
         "招商银行和中信证券2025年末员工人数各是多少？招行按含派遣人员口径，分别引用。"),
    ]
    for key, codes, question in comparisons:
        add("compare-" + key, "staff_comparison", question, codes)
    for key, question, code, dependency in [
        ("company", "那伊利股份的在职员工数量合计呢？", "600887", "single-000858"),
        ("switch", "换成海康威视，员工人数合计是多少？", "002415", "single-000651"),
        ("period", "沿用刚才的报告期，宝钢股份员工人数合计有多少人？", "600019", "single-600900"),
        ("same", "还是这家公司，2025年在职员工数量合计呢？", "600030", "single-600030"),
    ]:
        add("follow-" + key, "staff_followup", question, [code], depends=dependency)
    add("finance-wuliang", "financial_answerable", "五粮液2025年的全年营收是多少？",
        ["000858"], metric="revenue")
    add("finance-hengrui", "financial_answerable",
        "请按年报披露，给出恒瑞医药2025年营业收入及原始单位。", ["600276"], metric="revenue")
    add("finance-catl-follow", "financial_answerable", "那营收呢？", ["300750"],
        depends="single-300750", metric="revenue")
    add("finance-yili-follow", "financial_answerable", "只看伊利股份，它2025年营业收入是多少？",
        ["600887"], depends="compare-dairy-solar", metric="revenue")
    for index, question in enumerate([
        "招商银行2025年员工人数中，取得潜水证的员工有多少人？",
        "宝钢股份2025年在职员工数量中，会演奏大提琴的共有多少人？",
        "宁德时代2025年员工人数中，拥有私人飞行执照的有多少人？",
        "中国联通2025年员工数量中，参加过南极科考的有多少人？",
        "美的集团2025年员工人数按星座统计，水瓶座员工有多少人？",
        "五粮液2025年在职员工会三门外语的有多少人？",
        "恒瑞医药2025年在职员工中，出生在星期一的有多少人？",
        "中国石油2025年员工人数按惯用手分组，双手都灵活的有多少人？",
    ], 1):
        add(f"refuse-{index}", "no_evidence_refusal", question, mode="refused")
    for key, question, mode, evidence in [
        ("company", "请列出2025年的员工人数并附引用。", "clarification", False),
        ("period", "宝钢股份在职员工数量合计是多少？", "clarification", False),
        ("old-year", "伊利股份2021年员工人数合计是多少？", "refused", False),
        ("forecast", "请预测宁德时代下个月股价能涨到多少元。", "refused", False),
        ("market", "五粮液现在的市盈率是多少？", "unsupported", False),
        ("uncovered", "688981公司2025年员工人数合计是多少？", "refused", False),
        ("evidence", "宁德时代2025年的营收是多少？", "evidence_only", True),
        ("unreviewed-finance", "比亚迪2025年归属于上市公司股东的净利润是多少？",
         "evidence_only", False),
    ]:
        add("control-" + key, "routing_control", question, evidence=evidence, mode=mode)
    for code, name in [("600519", "贵州茅台"), ("601088", "中国神华"), ("002594", "比亚迪")]:
        add("known-" + code, "known_staff_regression", f"{name}2025年在职员工数量合计是多少人？",
            [code])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdftoppm", type=Path)
    args = parser.parse_args()
    started = perf_counter()
    names = ["eval/answer_acceptance_v2.jsonl", "eval/answer_acceptance_v2_gold.json",
             "eval/answer_acceptance_v2_contract.json", "eval/answer_acceptance_v2_audit.json"]
    if any((ROOT / p).exists() for p in names):
        raise RuntimeError("v2验收输入已存在，禁止覆盖")
    logging.basicConfig(filename=ROOT / "docs/retrospective/evidence/phase15-parser-warnings.log",
                        level=logging.WARNING)
    records = cases()
    seen_path = ROOT / "eval/answer_acceptance_v2_seen_inventory.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))["question_hashes"])
    duplicates = [c["id"] for c in records if c["group"] != "known_staff_regression"
                  and hashlib.sha256(normalized(c["question"]).encode()).hexdigest() in seen]
    assert not duplicates, duplicates
    assert len(records) == 43 and len({c["id"] for c in records}) == 43
    gold_path = ROOT / "eval/answer_acceptance_v1_gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    required = {key for c in records for key in c["fact_ids"]}
    facts = [f for f in gold["facts"] if f["fact_id"] in required]
    assert {f["fact_id"] for f in facts} == required
    proofs = []
    for fact in facts:
        path = ROOT / fact["pdf_path"]
        assert sha(path) == fact["pdf_sha256"]
        with pymupdf.open(path) as pdf:
            text = pdf[fact["physical_page"] - 1].get_text(sort=True)
            assert hashlib.sha256(text.encode()).hexdigest() == fact["primary_page_text_sha256"]
        independent = PdfReader(path).pages[fact["physical_page"] - 1].extract_text() or ""
        method = "pypdf-6.12.1"
        if not (normalized(fact["row_label"]) in normalized(independent)
                and normalized(fact["value"]) in normalized(independent)):
            executable = str(args.pdftoppm) if args.pdftoppm else shutil.which("pdftoppm")
            if not executable:
                raise RuntimeError("独立解析校验失败且没有Poppler")
            assert fact["fact_id"] == "600036:2025:headcount_total"
            prefix = ROOT / "docs/retrospective/evidence/phase15-bank-p103"
            subprocess.run([
                executable, "-f", "103", "-l", "103", "-singlefile", "-scale-to", "1400",
                "-png", str(path), str(prefix),
            ], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
            method = "pypdf-value-plus-Poppler-visual-label-required"
        else:
            assert normalized(fact["row_label"]) in normalized(independent), fact["fact_id"]
        assert normalized(fact["value"]) in normalized(independent), fact["fact_id"]
        proofs.append({"fact_id": fact["fact_id"], "pdf_sha256": fact["pdf_sha256"],
                       "physical_page": fact["physical_page"], "independent_method": method,
                       "independent_text_sha256": hashlib.sha256(independent.encode()).hexdigest(),
                       "prior_visual_crop": fact["screenshot"],
                       "crop_sha256": sha(ROOT / fact["screenshot"])})
    previous = json.loads((ROOT / "eval/answer_acceptance_v1_contract.json").read_text(
        encoding="utf-8"))
    contract = {**previous, "version": "document-answer-acceptance-v2-question-composition",
                "date": "2026-10-26", "planned_counts": dict(Counter(c["group"] for c in records)),
                "main_answer_denominator": 24, "staff_answer_denominator": 20,
                "financial_answer_denominator": 4,
                "scope": "Same-assistant one-shot scoped question/composition acceptance; "
                         "not personnel-isolated or unseen-fact validation",
                "novelty": "40 new normalized-exact question texts plus 3 separately scored known "
                           "controls; all answer facts/documents/companies seen in development. "
                           "No unseen company/year/metric generalization claim. Sealed v4 unread.",
                "gold_standard": "Carry prior original-PDF visual gold with fresh PDF hash, "
                                 "primary text hash and independent pypdf recheck; "
                                 "bank pypdf label garbled, fresh Poppler visual check required. "
                                 "No runtime QA facts, model answers or retrieval output used.",
                "api_calls": "At most 27 planned answer/known generation attempts; no retries; "
                             "unexpected refusal-case generation counted separately, max 35 total",
                "additional_gates": {"financial_answer_success_min": 0.9,
                                     "retrieved_gold_fact_coverage_min": 0.9}}
    contract["planned_counts"]["total"] = len(records)
    contract["scoring"] = {**previous["scoring"],
        "financial": "4 scoped revenue questions, 2 direct plus 2 context switches; "
                     "natural wording eligible by business scope, policy abstention still failure",
        "refusal": "8 unsupported employee-conjunction conditions; total is not a substitute. "
                   "Strict refused/zero claims, safe abstention separately; not LLM-only refusal"}
    audit = {"question_count": 43, "novel_question_count": 40, "known_regression_count": 3,
             "normalized_exact_duplicates_against_seen_inventory": duplicates,
             "seen_inventory_sha256": sha(seen_path), "previous_gold_sha256": sha(gold_path),
             "fact_count": len(facts), "fact_level_overlap_with_development": len(facts),
             "new_fact_count": 0, "source_pdf_rechecks": proofs, "api_calls": 0,
             "retrieval_calls": 0, "runtime_application_modified": False,
             "author_and_reviewer": "same_assistant", "sealed_v4_read": False,
             "local_preparation_seconds": perf_counter() - started}
    outputs = ["".join(json.dumps(c, ensure_ascii=False) + "\n" for c in records),
               json.dumps({"version": "pdf-gold-v2-carried-and-revalidated",
                           "provenance": contract["gold_standard"], "facts": facts},
                          ensure_ascii=False, indent=2) + "\n",
               json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
               json.dumps(audit, ensure_ascii=False, indent=2) + "\n"]
    for name, content in zip(names, outputs, strict=True):
        with (ROOT / name).open("x", encoding="utf-8") as stream:
            stream.write(content)
    print(json.dumps({"counts": contract["planned_counts"], "gold_facts": len(facts),
                      "fresh_fact_count": 0, "exact_question_overlap": duplicates}))


if __name__ == "__main__":
    main()
