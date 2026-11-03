import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.qa.config import QASettings
from app.qa.deepseek import DeepSeekClient, GenerationError
from app.qa.evidence import validate_draft
from app.qa.sessions import SessionStore
from app.qa.workflow import DocumentQA


def source():
    return {"source_id": "S1", "document_id": "601088-2025-annual", "stock_code": "601088",
            "report_period": "2025", "document_type": "annual_report", "section": "员工情况",
            "content": "在职员工数量合计（人）100", "layout_review_required": False}


def draft():
    return {"status": "answer", "claims": [{
        "text": "中国神华2025年在职员工数量为100人。", "stock_code": "601088",
        "report_period": "2025", "citations": [
            {"source_id": "S1", "quote": "在职员工数量合计（人）100"}
        ],
    }]}


RESOLVED = {"query": "中国神华2025年在职员工数量是多少？",
            "stock_codes": ["601088"], "report_period": "2025"}


def test_literal_checks_do_not_claim_financial_semantic_acceptance():
    _, checks = validate_draft(draft(), [source()], RESOLVED)
    assert checks["checks_passed"]
    assert not checks["financial_row_column_semantics_verified"]
    assert not checks["generated_answer_accuracy_accepted"]


def test_multi_year_column_alignment_rejects_historical_value_for_current_year():
    # 原失败反例保留，2024列的100不能再被当作2025列的200。
    s, d = source(), draft()
    s.update(section="主要会计数据", content="单位：百万元\n项目 2025年 2024年\n营业收入 200 100")
    d["claims"][0].update(text="2025年营业收入100百万元", citations=[
        {"source_id": "S1", "quote": s["content"]}
    ])
    _, checks = validate_draft(d, [s], RESOLVED)
    assert not checks["checks_passed"]
    assert "claim_1_value_year_column_mismatch" in checks["errors"]
    assert not checks["financial_row_column_semantics_verified"]
    assert not checks["generated_answer_accuracy_accepted"]


@pytest.mark.parametrize("change,error", [
    ("number", "unsupported_number"), ("quote", "quote_not_in_source"),
    ("source", "unknown_source"), ("company", "unexpected_company"),
    ("period", "unexpected_report_period"), ("unit", "unsupported_unit"),
])
def test_wrong_citations_and_fields_are_rejected(change, error):
    payload = draft()
    claim = payload["claims"][0]
    if change == "number":
        claim["text"] = "在职员工1000人"
    elif change == "quote":
        claim["citations"][0]["quote"] = "原文没有这句话"
    elif change == "source":
        claim["citations"][0]["source_id"] = "S99"
    elif change == "company":
        claim["stock_code"] = "600519"
    elif change == "period":
        claim["report_period"] = "2024"
    else:
        claim["text"] = "该金额100亿元"
    _, checks = validate_draft(payload, [source()], RESOLVED)
    assert not checks["checks_passed"]
    assert any(error in e for e in checks["errors"])


def test_same_pdf_company_metadata_does_not_justify_subsidiary_financial_table():
    s, d = source(), draft()
    s.update(section="财务公司主要财务指标", content="营业收入4,251百万元")
    d["claims"][0].update(text="营业收入4,251百万元", citations=[
        {"source_id": "S1", "quote": "营业收入4,251百万元"}
    ])
    _, checks = validate_draft(d, [s], {**RESOLVED, "query": "中国神华营业收入"})
    assert "claim_1_ambiguous_entity_scope" in checks["errors"]


def test_historical_year_in_claim_is_not_accepted_just_because_same_page_has_it():
    s, d = source(), draft()
    s["content"] += "，2024年为100人"
    d["claims"][0]["text"] = "2024年员工100人"
    d["claims"][0]["citations"][0]["quote"] = s["content"]
    _, checks = validate_draft(d, [s], RESOLVED)
    assert "claim_1_unexpected_year_in_text" in checks["errors"]


def test_deepseek_http_contract_and_secret_not_in_returned_payload():
    secret = "fake-unit-test-key"
    settings = QASettings(deepseek_api_key=SecretStr(secret))

    def handler(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer " + secret
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert "JSON" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(draft(), ensure_ascii=False)
        }}], "usage": {"total_tokens": 99}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        payload = DeepSeekClient(settings, client).generate("问题", [source()], RESOLVED)
    assert payload["draft"] == draft()
    assert payload["usage"]["total_tokens"] == 99
    assert secret not in json.dumps(payload)
    assert secret not in repr(settings)


def test_provider_error_body_is_not_exposed():
    secret = "fake-secret-in-error-body"
    settings = QASettings(deepseek_api_key=SecretStr(secret))
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(401, text=secret)
    )) as client:
        with pytest.raises(GenerationError, match="HTTP 401") as error:
            DeepSeekClient(settings, client).generate("问题", [], RESOLVED)
    assert secret not in str(error.value)


@pytest.mark.parametrize("content,finish", [("", "stop"), ("{", "stop"), ("{}", "length")])
def test_empty_broken_and_truncated_provider_outputs_fail(content, finish):
    settings = QASettings(deepseek_api_key=SecretStr("fake"))
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": content}}]
    }))) as client:
        with pytest.raises(GenerationError):
            DeepSeekClient(settings, client).generate("问题", [], RESOLVED)


class Runtime:
    def __init__(self, broken=False):
        self.broken = broken

    def metadata(self):
        return {"validated_strategy": True, "device": "cuda"}

    def retrieve_resolved(self, resolved):
        if self.broken:
            raise RuntimeError("reranker-unavailable")
        return [SimpleNamespace(
            text=source()["content"], same_page_supporting_chunks=[], document_type="annual_report",
            document_id="601088-2025-annual", stock_code="601088", stock_name="中国神华",
            report_period="2025", title="年报", page_start=84, source_url="https://example.com/a.pdf",
            section="员工情况", table_layout_review_required=False,
        )], []


class Generator:
    def __init__(self, configured=True, payload=None, broken=False):
        self.configured, self.payload, self.broken = configured, payload or draft(), broken

    def generate(self, *args):
        assert self.configured
        if self.broken:
            raise GenerationError("生成接口HTTP 401")
        return {"draft": copy.deepcopy(self.payload), "model": "mock-model", "usage": {}}


def qa(generator=None, runtime=None):
    return DocumentQA(lambda: runtime or Runtime(), generator or Generator(),
                      supported_codes={"601088"}, supported_periods={"2025"})


def test_graph_without_key_is_explicit_evidence_mode():
    response = qa(Generator(configured=False)).ask(RESOLVED["query"])
    assert response["mode"] == "evidence_only"
    assert response["sources"] and "claims" not in response


def test_graph_validated_quotes_are_still_labelled_draft():
    response = qa().ask(RESOLVED["query"])
    assert response["mode"] == "draft"
    assert response["validation"]["checks_passed"]
    assert not response["generated_answer_accuracy_accepted"]


def test_graph_does_not_pay_for_generation_when_no_fact_can_be_bound():
    class AmbiguousRuntime(Runtime):
        def retrieve_resolved(self, resolved):
            hits, tasks = super().retrieve_resolved(resolved)
            hits[0].text = "单位：百万元\n2025年 2024年 增减(%)\n营业收入 200 100 100"
            hits[0].section = "主要会计数据"
            return hits, tasks

    response = qa(Generator(broken=True), AmbiguousRuntime()).ask("中国神华2025年营业收入多少")
    assert response["mode"] == "evidence_only" and not response["sources"][0]["facts"]


def test_bad_numbers_are_not_displayed_as_generated_claims():
    payload = draft()
    payload["claims"][0]["text"] = "员工1000人"
    response = qa(Generator(payload=payload)).ask(RESOLVED["query"])
    assert response["mode"] == "validation_failed" and "claims" not in response


def test_graph_provider_error_keeps_evidence_but_does_not_fake_a_success():
    response = qa(Generator(broken=True)).ask(RESOLVED["query"])
    assert response["mode"] == "generation_error"
    assert response["sources"] and "claims" not in response


def test_model_insufficient_evidence_response_is_refusal_not_empty_success():
    response = qa(Generator(payload={"status": "insufficient_evidence", "claims": []})).ask(
        RESOLVED["query"]
    )
    assert response["mode"] == "refused"
    assert "claims" not in response


def test_model_cannot_mix_refusal_status_with_unverified_claims():
    payload = draft()
    payload["status"] = "insufficient_evidence"
    response = qa(Generator(payload=payload)).ask(RESOLVED["query"])
    assert response["mode"] == "validation_failed"
    assert "claims" not in response


def test_graph_reranker_error_does_not_fall_back():
    with pytest.raises(RuntimeError, match="reranker-unavailable"):
        qa(runtime=Runtime(broken=True)).ask(RESOLVED["query"])


@pytest.mark.parametrize("question,mode", [
    ("员工数量是多少", "clarification"), ("中国神华员工数量是多少", "clarification"),
    ("中国神华2026年员工数量多少", "refused"), ("中国神华明年股价能涨到多少", "refused"),
    ("中国神华当前PE是多少", "unsupported"),
    ("中国神华管理层给出的2027年股票目标价是多少", "refused"),
    ("中国神华下个月涨幅一定最高吗", "refused"),
])
def test_graph_routes_missing_context_and_unsupported_questions_before_retrieval(question, mode):
    assert qa(runtime=Runtime(broken=True)).ask(question)["mode"] == mode


def test_persisted_session_followup_and_reset_do_not_reuse_old_context(tmp_path):
    path = tmp_path / "sessions.sqlite"
    store = SessionStore(path)
    session = store.create()
    store.append(session, RESOLVED["query"], {"mode": "evidence_only"})
    reopened = SessionStore(path)
    followup = qa().ask("那在职员工数量是多少？", reopened.history(session))
    assert followup["resolved"]["stock_codes"] == ["601088"]
    assert followup["mode"] == "draft"
    reopened.reset(session)
    assert qa().ask("那在职员工数量是多少？", reopened.history(session))["mode"] == "clarification"
