import json

import httpx
import pytest
from pydantic import SecretStr

from app.qa.config import QASettings
from app.qa.deepseek import DeepSeekClient, GenerationError
from app.qa.diagnostics import FailureRecorder
from app.qa.resolution import resolve_document_query
from app.qa.workflow import DocumentQA


@pytest.mark.parametrize("question", [
    "2025年末平安体系内的员工人数为多少？",
    "平安体系2025年的在职员工总人数？",
    "中国平安体系内2025年员工数量？",
])
def test_only_qualified_system_alias_maps_to_ping_an(question):
    r = resolve_document_query(question)
    assert r["stock_codes"] == ["601318"] and r["report_period"] == "2025"
    assert question in r["query"]


@pytest.mark.parametrize("question", ["平安2025年员工人数？", "平安银行2025年员工人数？"])
def test_bare_or_bank_alias_does_not_inherit_unrelated_company(question):
    history = [{"role": "user", "content": "宁德时代2025年员工人数多少？"}]
    assert not resolve_document_query(question, history)["stock_codes"]


def test_system_followup_uses_qualified_history_without_turning_into_other_scope():
    r = resolve_document_query("那2025年呢？", [
        {"role": "user", "content": "平安体系2024年员工人数多少？"},
    ])
    assert r["stock_codes"] == ["601318"] and r["report_period"] == "2025"
    # 只有公司/年度继承；没有显式体系的追问不会借用体系人数。
    assert "体系" not in r["query"]


@pytest.mark.parametrize("question", [
    "五粮液2025年在职员工会三门外语的有多少人？",
    "五粮液2025年会英语的员工有多少人？",
    "五粮液2025年员工人数里养猫的有多少？",
    "五粮液2025年员工人数剔除派遣是多少？",
    "五粮液2025年博士员工人数多少？",
])
def test_subset_refusal_happens_before_retrieval_even_without_generator(question):
    class NoGen:
        configured = False

    qa = DocumentQA(lambda: pytest.fail("must not retrieve"), NoGen(),
                    supported_codes={"000858"}, supported_periods={"2025"})
    result = qa.ask(question)
    assert result["mode"] == "refused" and not result.get("claims")


@pytest.mark.parametrize("exception,code", [
    (httpx.ConnectTimeout("secret-response"), "provider_timeout"),
    (httpx.ReadTimeout("secret-response"), "provider_timeout"),
    (httpx.ConnectError("secret-response"), "provider_network"),
])
def test_network_error_subtypes_are_safe_and_not_automatically_retried(exception, code):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generator = DeepSeekClient(QASettings(deepseek_api_key=SecretStr("fake-key")), client)
        with pytest.raises(GenerationError) as error:
            generator.generate("问题", [], {})
    assert len(calls) == 1 and error.value.code == code
    assert "secret-response" not in str(error.value)


def test_rejected_draft_is_local_only_and_secret_is_redacted(tmp_path):
    recorder = FailureRecorder(tmp_path, secrets=("fake-api-key",))
    qa = DocumentQA(None, None, supported_codes={"601088"}, supported_periods={"2025"},
                    failure_recorder=recorder)
    state = {
        "question": "员工人数？", "resolved": {"query": "2025年员工人数",
                                               "stock_codes": ["601088"], "report_period": "2025"},
        "sources": [], "deployment": {}, "draft_payload": {
            "draft": {"status": "answer", "claims": [{"text": "fake-api-key错误草稿",
                "stock_code": "601088", "report_period": "2025", "citations": [
                    {"source_id": "S1", "quote": "不存在的引用"}]}]},
            "model": "stub", "usage": {},
        },
    }
    result = qa.validate(state)["result"]
    assert result["mode"] == "validation_failed" and "claims" not in result
    path = tmp_path / (result["diagnostic_id"] + ".json")
    assert "fake-api-key" not in path.read_text(encoding="utf-8")
    assert "错误草稿" in path.read_text(encoding="utf-8")
    assert "错误草稿" not in json.dumps(result, ensure_ascii=False)


def test_diagnostic_write_failure_does_not_mask_rejection(tmp_path):
    path = tmp_path / "not-directory"
    path.write_text("file", encoding="utf-8")
    assert FailureRecorder(path).record("validation_failed", {"draft": {}}) is None


def test_deepseek_total_headcount_uses_canonical_metric_instruction():
    def handler(request):
        body = json.loads(request.content)
        assert "在职员工总人数为X人" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"status":"insufficient_evidence","claims":[]}'}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generator = DeepSeekClient(QASettings(deepseek_api_key=SecretStr("fake")), client)
        generator.generate("问题", [], {})
