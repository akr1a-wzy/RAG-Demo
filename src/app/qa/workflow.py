"""LangGraph文档问答图；无Key为证据模式，生成结果仅为待语义验收草稿。"""

import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.qa.deepseek import GenerationError
from app.qa.employees import unsupported_employee_request
from app.qa.evidence import evidence_sources, validate_draft
from app.qa.facts import VERSION, metric_in, source_facts
from app.qa.financial import QA_POLICY
from app.qa.requests import financial_request
from app.qa.resolution import resolve_document_query
from app.qa.selection import select_target_facts
from app.tools.ticker import CODE_TO_CANONICAL_NAME

EMPLOYEE_QUERY_VERSION = "canonical-total-employee-v4-scope-clarification"


class QAState(TypedDict, total=False):
    question: str
    history: list[dict]
    resolved: dict
    next_step: str
    sources: list[dict]
    deployment: dict
    fact_selection: dict
    draft_payload: dict
    result: dict


def result(mode: str, message: str, **kwargs) -> dict:
    return {"mode": mode, "message": message, "generated_answer_accuracy_accepted": False,
            **kwargs}


class DocumentQA:
    def __init__(self, runtime_factory, generator, *, supported_codes: set[str],
                 supported_periods: set[str], review_provider=None, failure_recorder=None) -> None:
        self.runtime_factory, self.generator = runtime_factory, generator
        self.supported_codes, self.supported_periods = supported_codes, supported_periods
        self.review_provider = review_provider
        self.failure_recorder = failure_recorder
        self._runtime = None
        builder = StateGraph(QAState)
        builder.add_node("route", self.route)
        builder.add_node("retrieve", self.retrieve)
        builder.add_node("generate", self.generate)
        builder.add_node("validate", self.validate)
        builder.add_edge(START, "route")
        builder.add_conditional_edges("route", lambda s: s["next_step"],
                                      {"retrieve": "retrieve", "finish": END})
        builder.add_conditional_edges("retrieve", lambda s: s["next_step"],
                                      {"generate": "generate", "finish": END})
        builder.add_conditional_edges("generate", lambda s: s["next_step"],
                                      {"validate": "validate", "finish": END})
        builder.add_edge("validate", END)
        self.graph = builder.compile()

    def route(self, state: QAState) -> dict:
        question = state["question"].strip()
        if not question or len(question) > 2000:
            raise ValueError("问题长度须为1至2000字符")
        resolved = resolve_document_query(question, state.get("history", []))
        if re.search(r"营业收入|营收", resolved["query"]):
            resolved["financial_request"] = financial_request(resolved)
        outcome = None
        if re.search(
            r"预测|会不会涨|能涨到|股价翻倍|买入建议|卖出建议|明年.*股价|目标价"
            r"|下个?月.*(?:涨幅|股价|收益)|(?:涨幅|收益).*(?:一定|最高)", question,
        ):
            outcome = result("refused", "现有文档证据不能支持股价预测或买卖建议。")
        elif re.search(
            r"实时|现价|今日股价|今天.*股价|当前股价|市盈率|市净率|市值|PE|PB|日K|K线",
            question, re.I,
        ):
            outcome = result("unsupported", "行情与日K工作流尚未接入，不能用年报替代实时数据。")
        elif not resolved["stock_codes"]:
            outcome = result("clarification", "请指定公司或股票代码；当前仅覆盖已登记的20家公司。")
        elif not set(resolved["stock_codes"]) <= self.supported_codes:
            outcome = result("refused", "当前发布库不覆盖该公司，无法提供文档证据。")
        elif not resolved["report_period"]:
            outcome = result("clarification", "请指定报告期，例如2025年。")
        elif resolved["report_period"] not in self.supported_periods:
            outcome = result("refused", "当前发布语料不覆盖该报告期，无法据此回答。")
        elif unsupported_employee_request(resolved["query"]):
            outcome = result("refused", "当前证据目录不支持该员工细分条件，不能用员工合计替代。")
        return {"resolved": resolved, "next_step": "finish" if outcome else "retrieve",
                **({"result": outcome} if outcome else {})}

    def retrieve(self, state: QAState) -> dict:
        if self._runtime is None:
            self._runtime = self.runtime_factory()
        # 检索/GPU/重排异常直接传播，不退回未重排或旧库。
        resolved = state["resolved"]
        request = financial_request(resolved)
        retrieval_input = resolved
        if request["status"] == "supported":
            retrieval_input = {**resolved, "query": request["canonical_query"]}
        elif metric_in(resolved["query"]) == "headcount_total":
            # 只规范已识别的合计指标；主体/细分仍用原问题筛选和验证。
            names = "、".join(CODE_TO_CANONICAL_NAME.get(c, c) for c in resolved["stock_codes"])
            retrieval_input = {**resolved, "query":
                f"{names}{resolved['report_period']}年在职员工数量合计是多少人？"}
        hits, _ = self._runtime.retrieve_resolved(retrieval_input)
        sources = evidence_sources(hits, self.review_provider)
        deployment = {**self._runtime.metadata(), "qa_evidence_policy": QA_POLICY,
                      "binding_version": VERSION, "employee_query_policy": EMPLOYEE_QUERY_VERSION}
        common = {"sources": sources, "deployment": deployment}
        if not sources and request["status"] != "supported":
            return {**common, "next_step": "finish",
                    "result": result("refused", "没有检索到可用来源，无法回答。", **common)}
        sources, selection = select_target_facts(sources, state["resolved"])
        if request["status"] == "supported":
            selection["financial_request"] = request
            if selection["status"] == "missing" and len(selection["missing_companies"]) <= 2:
                sources, selection = self.supplement_financial_sources(sources, resolved, selection)
        if (selection["status"] == "missing" and selection["metric"] == "headcount_total"
                and len(selection["missing_companies"]) <= 2):
            sources, selection = self.supplement_employee_sources(
                sources, state["resolved"], selection,
            )
        selection["retrieval_queries"] = [retrieval_input["query"], *[
            a["query"] for a in selection.get("query_supplements", [])
        ]]
        common.update(sources=sources, fact_selection=selection)
        if not sources:
            return {**common, "next_step": "finish", "result": result(
                "refused", "没有检索到可用来源，无法回答。", **common
            )}
        if not self.generator.configured:
            return {**common, "next_step": "finish", "result": result(
                "evidence_only", "按证据模式展示原文，本次未调用生成接口。", **common
            )}
        if selection["status"] == "missing" and selection.get("metric") == "headcount_total":
            scopes = {f.get("entity_scope") for s in sources
                      for f in source_facts(s, self.review_provider)
                      if f["metric"] == "headcount_total" and
                      f["period"] == resolved["report_period"] and
                      f["stock_code"] in selection.get("missing_companies", [])}
            options = []
            if "filing_company" in scopes and "本公司" not in resolved["query"]:
                options.append("本公司员工人数")
            if "system" in scopes and "体系" not in resolved["query"]:
                options.append("体系内员工人数")
            if options:
                return {**common, "next_step": "finish", "result": result(
                    "clarification", "请明确员工主体口径：" + "、".join(options) +
                    "。可在问题中加入该口径后继续查询。", **common
                )}
        if selection["status"] == "unsupported_employee_subset":
            return {**common, "next_step": "finish", "result": result(
                "refused", "当前证据目录不支持该员工细分条件，不能用员工合计替代。", **common
            )}
        if selection["status"] != "ready":
            return {**common, "next_step": "finish", "result": result(
                "evidence_only", "目标事实缺失或口径冲突，仅展示原文，不生成不完整答案。", **common
            )}
        return {**common, "next_step": "generate"}

    def supplement_financial_sources(self, sources, resolved, selection):
        """每个缺失公司最多一次通用主表字段补查，仍须通过完整单元格审核。"""
        extras, attempts = [], []
        request = financial_request(resolved)
        for code in selection["missing_companies"]:
            name = CODE_TO_CANONICAL_NAME.get(code, code)
            query = f"{name}{resolved['report_period']}年营业收入 主要会计数据和财务指标"
            hits, _ = self._runtime.retrieve_resolved({
                **resolved, "query": query, "stock_codes": [code],
            })
            candidates, status = select_target_facts(
                evidence_sources(hits, self.review_provider), {**resolved, "stock_codes": [code]},
            )
            found = next((s for s in candidates if s["facts"]), None)
            attempts.append({"stock_code": code, "query": query, "status": status["status"],
                             "physical_page": found["physical_page"] if found else None})
            if status["status"] == "ready" and found:
                extras.append(found)
        if extras:
            sources, selection = self.merge_supplemental_sources(extras, sources, resolved)
        selection.update(financial_request=request, query_supplements=attempts)
        return sources, selection

    def merge_supplemental_sources(self, extras, sources, resolved):
        ordered = extras + [s for s in sources if s["facts"]]
        ordered += [s for s in sources if not s["facts"]]
        merged, seen = [], set()
        for source in ordered:
            key = source["document_id"], source["physical_page"]
            if key in seen:
                continue
            seen.add(key)
            source = {**source, "source_id": f"S{len(merged) + 1}"}
            source["facts"] = source_facts(source, self.review_provider)
            merged.append(source)
            if len(merged) == 5:
                break
        return select_target_facts(merged, resolved)

    def supplement_employee_sources(self, sources, resolved, selection):
        """缺少目标员工事实时，以通用字段表达补查；不改已发布检索参数或绕过复核。"""
        extras, attempts = [], []
        for code in selection["missing_companies"]:
            name = next((s["stock_name"] for s in sources if s["stock_code"] == code), code)
            query = f"{name}{resolved['report_period']}年本集团共有员工多少人？"
            rewritten = {**resolved, "query": query, "stock_codes": [code]}
            hits, _ = self._runtime.retrieve_resolved(rewritten)
            candidates, status = select_target_facts(
                evidence_sources(hits, self.review_provider), {**resolved, "stock_codes": [code]},
            )
            found = next((s for s in candidates if s["facts"]), None)
            attempts.append({"stock_code": code, "query": query, "status": status["status"],
                             "physical_page": found["physical_page"] if found else None})
            if status["status"] == "ready" and found:
                extras.append(found)
        if extras:
            sources, selection = self.merge_supplemental_sources(extras, sources, resolved)
        selection["query_supplements"] = attempts
        return sources, selection

    def generate(self, state: QAState) -> dict:
        try:
            payload = self.generator.generate(
                state["question"], state["sources"], state["resolved"]
            )
        except GenerationError as error:
            diagnostic_id = self.record_failure(
                "generation_error", state, {"error_code": error.code},
            )
            return {"next_step": "finish", "result": result(
                "generation_error", str(error), sources=state["sources"],
                deployment=state["deployment"], fact_selection=state.get("fact_selection", {}),
                error_code=error.code, diagnostic_id=diagnostic_id,
            )}
        return {"draft_payload": payload, "next_step": "validate"}

    def validate(self, state: QAState) -> dict:
        payload = state["draft_payload"]
        common = {"sources": state["sources"], "deployment": state["deployment"],
                  "model": payload["model"], "usage": payload["usage"],
                  "fact_selection": state.get("fact_selection", {})}
        try:
            draft, checks = validate_draft(payload["draft"], state["sources"], state["resolved"],
                                           self.review_provider, require_reviewed_financial=True)
        except ValidationError:
            diagnostic_id = self.record_failure("invalid_draft", state)
            return {"result": result(
                "validation_failed", "生成结构不合要求，未展示草稿。",
                diagnostic_id=diagnostic_id, **common
            )}
        if not checks["checks_passed"]:
            diagnostic_id = self.record_failure("validation_failed", state, {"validation": checks})
            return {"result": result("validation_failed", "引用或字段检查失败，未展示生成草稿。",
                                     validation=checks, diagnostic_id=diagnostic_id, **common)}
        if draft.status == "insufficient_evidence":
            return {"result": result("refused", "证据不足，无法给出可靠回答。",
                                     validation=checks, **common)}
        return {"result": result(
            "draft", "生成草稿：已检查引用和指标/年度/数值/单位绑定，答案准确率尚未验收。",
            claims=[c.model_dump() for c in draft.claims], validation=checks, **common,
        )}

    def ask(self, question: str, history: list[dict] | None = None) -> dict:
        final = self.graph.invoke({"question": question, "history": history or []})
        return {**final["result"], "resolved": final["resolved"]}

    def record_failure(self, kind: str, state: QAState, extra: dict | None = None):
        if self.failure_recorder is None:
            return None
        return self.failure_recorder.record(kind, {
            "question": state.get("question"), "resolved": state["resolved"],
            "sources": state["sources"], "deployment": state["deployment"],
            "draft_payload": state.get("draft_payload"), **(extra or {}),
        })
