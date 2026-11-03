import json
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.qa.config import QASettings
from app.retrieval.pdf_extract import file_sha256
from app.web.api import create_app
from app.web.service import DemoEngine, generation_allowed


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.fail = False

    def metadata(self):
        return {"device": "cuda", "validated_strategy": True}

    def ask(self, question, history, *, evidence_only=False):
        self.calls.append((question, history, evidence_only))
        if self.fail:
            raise RuntimeError("secret-that-must-not-be-logged")
        return {"mode": "evidence_only", "message": "原文证据", "sources": [],
                "resolved": {"stock_codes": ["601088"], "report_period": "2025"}}


@pytest.fixture
def web_project(tmp_path):
    pdf_path = tmp_path / "data/raw/test.pdf"
    pdf_path.parent.mkdir(parents=True)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), "Test PDF source")
        pdf.save(pdf_path)
    catalog = tmp_path / "data/manifests/documents.jsonl"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({
        "document_id": "test-report", "stock_code": "601088", "stock_name": "测试公司",
        "document_type": "annual_report", "title": "测试报告", "report_period": "2025",
        "publish_date": "2026-01-01", "source_url": "https://example.com/report.pdf",
        "local_path": "data/raw/test.pdf", "sha256": file_sha256(pdf_path), "page_count": 1,
    }) + "\n", encoding="utf-8")
    settings = QASettings(deepseek_api_key=SecretStr("backend-test-secret"),
                          qa_session_db=Path("data/test-sessions.sqlite"))
    engine = FakeEngine()
    return tmp_path, settings, engine


@pytest.fixture
def client(web_project):
    root, settings, engine = web_project
    with TestClient(create_app(root, settings=settings, engine_factory=lambda: engine)) as test:
        yield test


def session(client):
    response = client.post("/api/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def test_homepage_health_and_openapi_do_not_expose_backend_key(client):
    assert client.get("/").status_code == 200
    assert "有据" in client.get("/").text
    assert client.get("/api/health").json()["status"] == "ready"
    for url in ["/", "/api/health", "/openapi.json", "/static/app.js"]:
        assert "backend-test-secret" not in client.get(url).text
    assert client.get("/.env").status_code == 404
    assert "/api/chat" in client.get("/openapi.json").json()["paths"]


def test_session_chat_followup_and_reset(client, web_project):
    sid = session(client)
    first = client.post("/api/chat", json={"session_id": sid, "question": "中国神华2025年员工数量"})
    assert first.status_code == 200
    assert first.json()["generated_answer_accuracy_accepted"] is False
    second = client.post("/api/chat", json={"session_id": sid, "question": "那营业收入呢？",
                                          "evidence_only": True})
    assert second.status_code == 200
    assert web_project[2].calls[1][1][0]["content"] == "中国神华2025年员工数量"
    assert len(client.get(f"/api/sessions/{sid}").json()["turns"]) == 2
    assert client.delete(f"/api/sessions/{sid}/messages").json()["reset"] is True
    assert client.get(f"/api/sessions/{sid}").json()["turns"] == []


def test_market_payload_and_explicit_replay_are_preserved_in_session(web_project):
    root, settings, _ = web_project

    class MarketEngine(FakeEngine):
        def ask(self, question, history, *, evidence_only=False, market_replay=False):
            return {"mode": "market_data", "message": "行情源值", "market": {
                "kind": "quote", "data_mode": "replay" if market_replay else "live",
                "datasets": [{"quote": {"code": "600519", "price": "1349.98"}}],
            }}

    with TestClient(create_app(root, settings=settings, engine_factory=MarketEngine)) as client:
        sid = session(client)
        for replay in [False, True]:
            r = client.post("/api/chat", json={"session_id": sid, "question": "茅台最新股价",
                                             "market_replay": replay})
            assert r.status_code == 200 and r.json()["mode"] == "market_data"
            assert r.json()["market"]["data_mode"] == ("replay" if replay else "live")
            assert not r.json()["usage"] and not r.json()["claims"]
        turns = client.get(f"/api/sessions/{sid}").json()["turns"]
        assert turns[1]["answer"]["market"]["data_mode"] == "replay"


@pytest.mark.parametrize("question", [" ", "x" * 2001])
def test_invalid_questions_rejected_before_engine(client, web_project, question):
    response = client.post("/api/chat", json={"session_id": session(client), "question": question})
    assert response.status_code == 422
    assert web_project[2].calls == []


def test_unknown_session_and_bad_uuid_are_rejected(client):
    assert client.get(f"/api/sessions/{uuid4()}").status_code == 404
    assert client.get("/api/sessions/not-a-uuid").status_code == 422


def test_busy_inference_and_reset_are_explicit_errors(client):
    sid = session(client)
    lock = client.app.state.chat_lock
    lock.acquire()
    try:
        response = client.post("/api/chat", json={"session_id": sid, "question": "问题"})
        assert response.status_code == 429
        assert client.delete(f"/api/sessions/{sid}/messages").status_code == 409
    finally:
        lock.release()


def test_retrieval_error_does_not_leak_exception_or_create_fake_turn(client, web_project, caplog):
    sid = session(client)
    web_project[2].fail = True
    response = client.post("/api/chat", json={"session_id": sid, "question": "问题"})
    assert response.status_code == 503
    assert "secret-that-must-not-be-logged" not in response.text + caplog.text
    assert client.get(f"/api/sessions/{sid}").json()["turns"] == []


def test_startup_failure_keeps_ui_and_returns_unavailable(web_project):
    root, settings, _ = web_project

    def broken_engine():
        raise RuntimeError("GPU unavailable")

    with TestClient(create_app(root, settings=settings, engine_factory=broken_engine)) as test:
        assert test.get("/").status_code == 200
        assert test.get("/api/health").status_code == 503
        sid = session(test)
        response = test.post("/api/chat", json={"session_id": sid, "question": "问题"})
        assert response.status_code == 503


def test_cross_origin_write_and_untrusted_hosts_are_rejected(client):
    response = client.post("/api/sessions", headers={"Origin": "https://evil.invalid"})
    assert response.status_code == 403
    assert client.post("/api/sessions", headers={"Origin": "http://testserver"}).status_code == 201
    assert client.get("/api/health", headers={"Host": "evil.invalid"}).status_code == 400


def test_pdf_is_inline_and_cannot_read_arbitrary_paths(client, web_project):
    response = client.get("/documents/test-report.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.content.startswith(b"%PDF")
    assert client.get("/documents/unknown.pdf").status_code == 404
    partial = client.get("/documents/test-report.pdf", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206 and len(partial.content) == 100
    root, _, _ = web_project
    (root / "data/raw/test.pdf").write_bytes(b"modified")
    assert client.get("/documents/test-report.pdf").status_code == 409


class QAProbe:
    def __init__(self, mode):
        self.mode, self.calls = mode, 0

    def ask(self, question, history):
        self.calls += 1
        return {"mode": self.mode, "message": "probe"}


@pytest.mark.parametrize("question", [
    "中国神华2025年营业收入多少？", "员工人数和营业收入分别是多少？", "在职员工的工资预算多少？",
])
def test_financial_requests_cannot_bypass_server_evidence_policy(question):
    engine = object.__new__(DemoEngine)
    engine.qa, engine.evidence_qa = QAProbe("draft"), QAProbe("evidence_only")
    answer = engine.ask(question, [], evidence_only=False)
    assert answer["mode"] == "evidence_only"
    assert engine.qa.calls == 0 and engine.evidence_qa.calls == 1
    assert "未配置" not in answer["message"]


def test_headcount_generation_is_allowed_but_financial_followup_is_not():
    assert generation_allowed("中国神华2025年在职员工数量合计是多少人", [])
    history = [{"role": "user", "content": "中国神华2025年在职员工数量是多少"}]
    assert not generation_allowed("那营业收入呢", history)


def test_model_financial_output_is_hidden_even_for_a_headcount_question():
    class WrongScopeQA(QAProbe):
        def ask(self, question, history):
            return {"mode": "draft", "message": "probe", "claims": [{"text": "合成金额100亿元"}]}

    engine = object.__new__(DemoEngine)
    engine.qa, engine.evidence_qa = WrongScopeQA("draft"), QAProbe("evidence_only")
    response = engine.ask("中国神华2025年在职员工数量是多少", [])
    assert response["mode"] == "evidence_only"
    assert "claims" not in response


def test_scope_counts_and_pdf_map_only_include_indexed_documents(web_project):
    root, settings, engine = web_project
    catalog = root / "data/manifests/documents.jsonl"
    extra = json.loads(catalog.read_text(encoding="utf-8"))
    extra.update(document_id="not-indexed", stock_code="600519")
    with catalog.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(extra) + "\n")
    engine.document_ids = {"test-report"}
    with TestClient(create_app(root, settings=settings, engine_factory=lambda: engine)) as test:
        health = test.get("/api/health").json()
        assert health["companies"] == 1 and health["documents"] == 1
        assert test.get("/documents/not-indexed.pdf").status_code == 404


def test_release_change_requires_restart_instead_of_serving_stale_runtime(web_project):
    root, settings, engine = web_project
    engine.metadata = lambda: {"release_id": "release-before"}
    profile = root / "data/retrieval/active_profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"release_id": "release-before"}), encoding="utf-8")
    with TestClient(create_app(root, settings=settings, engine_factory=lambda: engine)) as test:
        assert test.get("/api/health").status_code == 200
        profile.write_text(json.dumps({"release_id": "release-after"}), encoding="utf-8")
        assert test.get("/api/health").status_code == 503
        sid = session(test)
        response = test.post("/api/chat", json={"session_id": sid, "question": "问题"})
        assert response.status_code == 503 and engine.calls == []
