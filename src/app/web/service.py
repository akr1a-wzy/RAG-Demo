"""网页演示策略：员工及选定营业收入可生成草稿，其余仅展示证据。"""

import json
import re
from pathlib import Path

from app.market.service import RUNTIME_VERSION as MARKET_VERSION
from app.market.service import MarketService, plan_market_request
from app.qa.catalog import VERSION as CATALOG_VERSION
from app.qa.catalog import DocumentCatalog
from app.qa.config import QASettings
from app.qa.deepseek import DeepSeekClient
from app.qa.diagnostics import FailureRecorder
from app.qa.facts import VERSION, metric_in
from app.qa.financial import FINANCIAL_VERSION, QA_POLICY, supported_financial_request
from app.qa.query_understanding import VERSION as QUERY_VERSION
from app.qa.query_understanding import capability_decision, understand_query
from app.qa.resolution import resolve_document_query
from app.qa.reviews import ReviewedFacts
from app.qa.workflow import EMPLOYEE_QUERY_VERSION, DocumentQA
from app.retrieval.incremental import RETRIEVAL_PROFILE
from app.retrieval.published import open_published_retriever, read_json

EXAMPLES = [
    {"label": "茅台 · 价格与估值", "question": "贵州茅台最新股价、PE、PB和市值是多少？",
     "kind": "market_data"},
    {"label": "茅台 · 20日前复权日K", "question": "贵州茅台最近20个交易日日K和涨跌幅",
     "kind": "market_data"},
    {"label": "神华 · 员工人数", "question": "中国神华2025年在职员工数量合计是多少人？",
     "kind": "draft"},
    {"label": "茅台 · 员工人数", "question": "贵州茅台2025年在职员工数量合计是多少人？",
     "kind": "draft"},
    {"label": "比亚迪 · 员工人数", "question": "比亚迪2025年在职员工数量合计是多少人？",
     "kind": "draft"},
    {"label": "神华 · 营收与同比", "question": "中国神华2025年的营业收入是多少，同比变化多少？",
     "kind": "structured_answer"},
    {"label": "宁德 · 财务概览", "question": "宁德时代2025年财务概览及原文",
     "kind": "structured_answer"},
    {"label": "茅台 · 分红与登记日", "question": "贵州茅台2025年分红及股权登记日是多少？",
     "kind": "structured_answer"},
    {"label": "预测问题 · 拒答", "question": "中国神华下个月股价一定会涨吗？",
     "kind": "refused"},
]


class NoGeneration:
    configured = False


def generation_allowed(question: str, history: list[dict], *, financial_scope=frozenset()) -> bool:
    resolved = resolve_document_query(question, history)
    query = resolved["query"]
    if supported_financial_request(resolved):
        return all((code, resolved["report_period"]) in financial_scope
                   for code in resolved["stock_codes"])
    return metric_in(query) == "headcount_total" and not re.search(
        r"收入|资产|利润|金额|现金|股利|分红|每股|薪酬|工资|成本|预算|费用|收益"
        r"|亿元|万元|百万元|构成|学历|专业|占比|比例|增长|增减|原因|母公司|子公司", query
    )


class DemoEngine:
    def __init__(self, root: Path, settings: QASettings) -> None:
        self.runtime = open_published_retriever(root)
        hybrid = getattr(self.runtime.retriever, "hybrid", self.runtime.retriever)
        self.document_ids = {c.document_id for c in hybrid.chunks.values()}
        profile = read_json(root / RETRIEVAL_PROFILE)
        chunks = root / profile["artifact_root"] / "data/processed/embedding_chunks.jsonl"
        periods = {json.loads(line)["report_period"]
                   for line in chunks.read_text(encoding="utf-8").splitlines() if line.strip()}
        reviews = ReviewedFacts(root)
        self.financial_scope = {(r["stock_code"], r["period"])
                                for r in reviews.providers[1].reviews}
        kwargs = {"supported_codes": set(profile["companies"]), "supported_periods": periods,
                  "review_provider": reviews, "failure_recorder": FailureRecorder(
                      root / "data/qa_diagnostics",
                      secrets=(settings.deepseek_api_key.get_secret_value(),),
                  )}
        self.qa = DocumentQA(lambda: self.runtime, DeepSeekClient(settings), **kwargs)
        self.evidence_qa = DocumentQA(lambda: self.runtime, NoGeneration(), **kwargs)
        self.generation_configured = settings.configured
        self.market = MarketService(root)
        self.catalog = DocumentCatalog(root)

    def metadata(self) -> dict:
        return {**self.runtime.metadata(), "qa_evidence_policy": QA_POLICY,
                "binding_version": VERSION, "financial_fact_version": FINANCIAL_VERSION,
                "market_version": MARKET_VERSION, "query_understanding_version": QUERY_VERSION,
                "employee_query_policy": EMPLOYEE_QUERY_VERSION,
                "document_catalog_version": CATALOG_VERSION}

    def ask(self, question: str, history: list[dict], *, evidence_only: bool = False,
            market_replay: bool = False) -> dict:
        query = understand_query(question, history)
        decision = capability_decision(query)
        if decision is not None:
            return {"mode": decision["status"], "message": decision["message"],
                    "resolved": {**query.resolved, "query_understanding": {
                        **query.metadata(), "capability": decision}},
                    "generated_answer_accuracy_accepted": False}
        market_request = plan_market_request(question, history, understanding=query)
        if market_request is not None:
            return self.market.ask(market_request, replay=market_replay)
        catalog = getattr(self, "catalog", None)
        if catalog is not None:
            answer = catalog.ask(question, history, evidence_only=evidence_only,
                                 understanding=query)
            if answer is not None:
                return answer
        allowed = generation_allowed(question, history,
                                     financial_scope=getattr(self, "financial_scope", frozenset()))
        evidence = evidence_only or not allowed
        answer = (self.evidence_qa if evidence else self.qa).ask(question, history)
        monetary_output = answer["mode"] == "draft" and any(re.search(
            r"收入|营收|资产|利润|现金|股利|分红|每股|薪酬|工资|成本|预算|费用"
            r"|亿元|万元|千元|人民币|元/股|\d\s*元", claim["text"]
        ) for claim in answer.get("claims", []))
        checks = answer.get("validation", {})
        financial_output_allowed = (
            allowed and supported_financial_request(resolve_document_query(question, history))
            and checks.get("checks_passed") and checks.get("selected_financial_cells_verified")
        )
        if monetary_output and not financial_output_allowed:
            answer["mode"] = "evidence_only"
            answer.pop("claims", None)
            answer["message"] = "草稿涉及暂未开放的财务金额，已改为仅展示原文证据。"
        if answer["mode"] == "evidence_only" and evidence:
            answer["message"] = (
                "按你的选择，仅展示原文证据，本次未调用生成接口。" if evidence_only
                else "本演示开放员工人数及已核对的年度营业收入；此问题仅展示原文证据。"
            )
        answer["demo_policy"] = {
            "generation_allowed": allowed, "requested_evidence_only": evidence_only,
            "financial_amounts": "reviewed_revenue_only",
        }
        answer.setdefault("resolved", {})["query_understanding"] = query.metadata()
        return answer
