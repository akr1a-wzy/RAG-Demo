import base64
import hashlib
from dataclasses import replace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.web.public import PublicBudget, PublicConfig, PublicEntry, load_config

ORIGIN = "https://portfolio.ngrok-free.app"
PASSWORD = "synthetic-test-password"


@pytest.fixture
def config(tmp_path):
    return PublicConfig(ORIGIN, "demo", hashlib.sha256(PASSWORD.encode()).hexdigest(), tmp_path)


def credential(password=PASSWORD):
    return "Basic " + base64.b64encode(f"demo:{password}".encode()).decode()


def entry(config):
    async def echo(request: Request):
        body = await request.body()
        return JSONResponse({"host": request.headers["host"],
                             "origin": request.headers.get("origin"),
                             "auth_leaked": "authorization" in request.headers,
                             "proxy_leaked": "x-forwarded-host" in request.headers,
                             "body": body.decode(), "range": request.headers.get("range")})

    return PublicEntry(Starlette(routes=[Route("/{path:path}", echo,
                                               methods=["GET", "POST", "DELETE"])]), config)


def test_every_public_resource_requires_access_code(config):
    with TestClient(entry(config), base_url=ORIGIN) as client:
        for path in ["/", "/api/health", "/api/sessions/example", "/documents/report.pdf",
                     "/static/app.js"]:
            result = client.get(path)
            assert result.status_code == 401
            assert result.headers["www-authenticate"].startswith("Basic")
            assert result.headers["cache-control"] == "private, no-store"
        for raw in ["Basic !!!", "Bearer password", credential("wrong"), "Basic ZGVtbw=="]:
            assert client.get("/", headers={"Authorization": raw}).status_code == 401
        assert client.get("/", headers={"Authorization": credential()}).status_code == 200


def test_public_origin_is_validated_before_private_header_normalization(config):
    with TestClient(entry(config), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        result = client.post("/api/sessions", content="{}", headers={"Origin": ORIGIN})
        assert result.status_code == 200
        assert result.json() == {"host": "127.0.0.1", "origin": "http://127.0.0.1",
                                 "auth_leaked": False, "proxy_leaked": False,
                                 "body": "{}", "range": None}
        for origin in ["https://evil.invalid", ORIGIN + ".evil.invalid", "null", ORIGIN + "/"]:
            assert client.post("/api/chat", headers={"Origin": origin}).status_code == 403
        assert client.get("/", headers={"Host": "evil.invalid",
                                        "X-Forwarded-Host": "portfolio.ngrok-free.app"}
                          ).status_code == 400
        result = client.post("/api/chat", headers={"Origin": ORIGIN,
                                                   "X-Forwarded-Host": "evil.invalid"})
        assert result.status_code == 200 and not result.json()["proxy_leaked"]


def test_pdf_range_is_preserved_and_internal_api_docs_hidden(config):
    with TestClient(entry(config), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        assert client.get("/documents/report.pdf", headers={"Range": "bytes=0-99"}
                          ).json()["range"] == "bytes=0-99"
        for path in ["/docs", "/redoc", "/openapi.json"]:
            assert client.get(path).status_code == 404


def test_authenticated_body_limit_and_invalid_content_length(config):
    with TestClient(entry(replace(config, max_body_bytes=8)), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        assert client.post("/api/chat", content=b"x" * 9).status_code == 413
        assert client.post("/api/chat", content=b"x", headers={"Content-Length": "9"}
                           ).status_code == 413
        assert client.post("/api/chat", headers={"Content-Length": "-1"}).status_code == 413
        assert client.post("/api/chat", headers={"Content-Length": "bad"}).status_code == 400
        assert client.post("/api/chat", content=b"{}").status_code == 200


def test_chunked_body_cannot_bypass_size_limit(config):
    def chunks():
        yield b"12345"
        yield b"67890"

    with TestClient(entry(replace(config, max_body_bytes=8)), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        assert client.post("/api/chat", content=chunks()).status_code == 413


def test_persistent_chat_budget_survives_entry_restart(config):
    config = replace(config, daily_chat_limit=1)
    with TestClient(entry(config), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        assert client.post("/api/chat", content="{}").status_code == 200
    with TestClient(entry(config), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        result = client.post("/api/chat", content="{}")
        assert result.status_code == 429 and int(result.headers["retry-after"]) > 0
        assert client.get("/").status_code == 200
        assert client.post("/api/sessions").status_code == 200


def test_unauthenticated_or_cross_origin_request_does_not_spend_budget(config):
    config = replace(config, daily_chat_limit=1)
    with TestClient(entry(config), base_url=ORIGIN) as client:
        assert client.post("/api/chat").status_code == 401
        client.headers["Authorization"] = credential()
        assert client.post("/api/chat", headers={"Origin": "https://evil.invalid"}
                           ).status_code == 403
        assert client.post("/api/chat").status_code == 200


def test_write_rate_is_global_and_not_bypassed_by_forwarded_ip(config):
    with TestClient(entry(replace(config, writes_per_minute=1)), base_url=ORIGIN,
                    headers={"Authorization": credential()}) as client:
        assert client.post("/api/sessions").status_code == 200
        result = client.post("/api/chat", headers={"X-Forwarded-For": "1.2.3.4"})
        assert result.status_code == 429 and result.headers["retry-after"] == "60"


def test_login_failure_throttle_does_not_lock_out_correct_password(config):
    with TestClient(entry(config), base_url=ORIGIN) as client:
        for _ in range(10):
            assert client.get("/").status_code == 401
        assert client.get("/").status_code == 429
        assert client.get("/", headers={"Authorization": credential()}).status_code == 200


def test_daily_budget_resets_on_beijing_date_boundary(config):
    budget = PublicBudget(replace(config, daily_chat_limit=1, writes_per_minute=100))
    # UTC 15:59:59, then 16:00:00 crosses midnight in Asia/Shanghai.
    now = 1762185599.0
    assert budget.reserve(chat=True, now=now) is None
    assert budget.reserve(chat=True, now=now) is not None
    assert budget.reserve(chat=True, now=now + 1) is None


@pytest.mark.parametrize("origin", ["http://demo.example", "https://demo.example/path",
                                   "https://user:pass@demo.example", "https://*.example",
                                   "https://demo.example?x=1"])
def test_invalid_public_configuration_is_rejected(config, origin):
    with pytest.raises(ValueError):
        replace(config, origin=origin)


def test_public_state_cannot_be_pointed_at_personal_database(tmp_path):
    import json

    path = tmp_path / "public.json"
    path.write_text(json.dumps({"origin": ORIGIN, "username": "demo",
                               "password_sha256": hashlib.sha256(PASSWORD.encode()).hexdigest(),
                               "state_dir": "data/personal"}), encoding="utf-8")
    with pytest.raises(ValueError, match="data/public_demo"):
        load_config(path, tmp_path)
    assert not (tmp_path / "data/personal").exists()


def test_public_factory_uses_isolated_sessions_and_unchanged_local_checks(tmp_path):
    import json

    from app.web.public import create_public_app

    manifests = tmp_path / "data/manifests"
    manifests.mkdir(parents=True)
    (manifests / "documents.jsonl").write_text("", encoding="utf-8")
    config = PublicConfig(ORIGIN, "demo", hashlib.sha256(PASSWORD.encode()).hexdigest(),
                          tmp_path / "data/public_demo")

    class FakeEngine:
        def metadata(self):
            return {}

        def ask(self, question, history, **kwargs):
            return {"mode": "evidence_only", "message": "synthetic source"}

    app = create_public_app(tmp_path, config, engine_factory=FakeEngine)
    with TestClient(app, base_url=ORIGIN, headers={"Authorization": credential()}) as client:
        sid = client.post("/api/sessions", headers={"Origin": ORIGIN}).json()["session_id"]
        result = client.post("/api/chat", json={"session_id": sid, "question": "synthetic"},
                             headers={"Origin": ORIGIN})
        assert result.status_code == 200
        assert client.get(f"/api/sessions/{sid}").json()["turns"][0]["question"] == "synthetic"
        assert client.delete(f"/api/sessions/{sid}/messages",
                             headers={"Origin": ORIGIN}).status_code == 200
        assert client.get(f"/api/sessions/{sid}").json()["turns"] == []
        assert client.get("/api/health").json()["status"] == "ready"
        redirect = client.post("/api/sessions/", headers={"Origin": ORIGIN},
                               follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == ORIGIN + "/api/sessions"
    assert (tmp_path / "data/public_demo/sessions.sqlite").is_file()
    assert not (tmp_path / "data/qa_sessions.sqlite").exists()
    assert PASSWORD not in json.dumps(app.app.openapi())
