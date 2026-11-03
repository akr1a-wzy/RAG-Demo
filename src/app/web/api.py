"""本机FastAPI入口：同源网页、会话、问答及白名单PDF查看。"""

import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.qa.config import QASettings
from app.qa.sessions import SessionStore
from app.retrieval.incremental import RETRIEVAL_PROFILE
from app.retrieval.pdf_extract import file_sha256, load_manifest
from app.web.service import EXAMPLES, DemoEngine

ROOT = Path(__file__).resolve().parents[3]
STATIC = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    evidence_only: bool = False
    market_replay: bool = False

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class ChatResponse(BaseModel):
    session_id: UUID
    request_id: str
    mode: Literal["draft", "evidence_only", "refused", "clarification", "unsupported",
                  "generation_error", "validation_failed", "market_data", "market_error",
                  "structured_answer"]
    message: str
    claims: list[dict] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    resolved: dict = Field(default_factory=dict)
    deployment: dict = Field(default_factory=dict)
    demo_policy: dict = Field(default_factory=dict)
    validation: dict = Field(default_factory=dict)
    fact_selection: dict = Field(default_factory=dict)
    generated_answer_accuracy_accepted: bool = False
    model: str | None = None
    usage: dict = Field(default_factory=dict)
    error_code: str | None = None
    diagnostic_id: str | None = None
    market: dict = Field(default_factory=dict)
    answer_method: str | None = None


class SessionResponse(BaseModel):
    session_id: UUID


def create_app(root: Path = ROOT, *, engine_factory=None, settings: QASettings | None = None):
    root = root.resolve()
    settings = settings or QASettings(_env_file=root / ".env")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        db = settings.qa_session_db
        application.state.store = SessionStore(db if db.is_absolute() else root / db)
        application.state.chat_lock = threading.Lock()
        application.state.documents = {
            d["document_id"]: d for d in load_manifest(root / "data/manifests/documents.jsonl")
        }
        application.state.engine = None
        try:
            factory = engine_factory or (lambda: DemoEngine(root, settings))
            application.state.engine = await run_in_threadpool(factory)
            ids = getattr(application.state.engine, "document_ids", None)
            if ids is not None:
                application.state.documents = {
                    key: value for key, value in application.state.documents.items() if key in ids
                }
        except Exception as error:
            # 不输出配置值、远端正文或密钥；失败不降级CPU/旧库。
            logging.getLogger(__name__).error("Demo startup failed: %s", type(error).__name__)
        yield

    application = FastAPI(
        title="A股文档问答Demo", version="0.1.0", lifespan=lifespan,
        description="本机文档问答；员工问答草稿、核准财务与公告字段、计算、行情及原文引用。",
    )
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @application.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            parsed = urlsplit(origin)
            if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
                return JSONResponse({"detail": "仅允许同源请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def check_session(session_id: str):
        try:
            application.state.store.ensure(session_id)
        except ValueError:
            raise HTTPException(404, "会话不存在，请新建会话") from None

    def current_engine():
        engine = application.state.engine
        if engine is None:
            return None
        loaded_id = engine.metadata().get("release_id")
        if loaded_id:
            try:
                active_id = json.loads((root / RETRIEVAL_PROFILE).read_text(
                    encoding="utf-8"))["release_id"]
            except (OSError, ValueError, KeyError):
                return None
            if loaded_id != active_id:
                return None
        return engine

    @application.get("/api/health")
    def health():
        engine = current_engine()
        data = {
            "status": "ready" if engine else "unavailable",
            "generation_configured": settings.configured,
            "documents": len(application.state.documents),
            "companies": len({d["stock_code"] for d in application.state.documents.values()}),
            "notice": "员工问答、核准财务/公告字段、同比/换算；行情、估值、前复权日K与手动回放。",
        }
        if engine:
            data["deployment"] = engine.metadata()
        return JSONResponse(data, status_code=200 if engine else 503)

    @application.get("/api/examples")
    def examples():
        return {"examples": EXAMPLES}

    @application.get("/api/coverage")
    def coverage():
        engine = current_engine()
        catalog = getattr(engine, "catalog", None)
        if catalog is None:
            raise HTTPException(503, "字段目录未就绪")
        return {"coverage": catalog.coverage(), "notice":
                "财务2024年为2025年报比较列，保留调整/重述口径。公告仅列本库已有来源，缺字段不推断为零。"}

    @application.post("/api/sessions", response_model=SessionResponse, status_code=201)
    def new_session():
        return {"session_id": application.state.store.create()}

    @application.get("/api/sessions/{session_id}")
    def session_history(session_id: UUID):
        sid = str(session_id)
        check_session(sid)
        with application.state.store.connect() as db:
            rows = db.execute(
                "SELECT question,result_json,created_at FROM turns WHERE session_id=? ORDER BY id",
                (sid,),
            ).fetchall()
        return {"session_id": sid, "turns": [
            {"question": r[0], "answer": json.loads(r[1]), "created_at": r[2]} for r in rows
        ]}

    @application.delete("/api/sessions/{session_id}/messages")
    def reset_session(session_id: UUID):
        sid = str(session_id)
        check_session(sid)
        lock = application.state.chat_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "问题仍在处理中，请完成后再重置")
        try:
            application.state.store.reset(sid)
        finally:
            lock.release()
        return {"session_id": sid, "reset": True}

    @application.post("/api/chat", response_model=ChatResponse)
    def chat(body: ChatRequest):
        sid = str(body.session_id)
        check_session(sid)
        engine = current_engine()
        if engine is None:
            raise HTTPException(503, "GPU检索服务未就绪，本次未生成答案")
        lock = application.state.chat_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(429, "已有问题正在处理，请稍后再试", headers={"Retry-After": "2"})
        try:
            options = {"evidence_only": body.evidence_only}
            if body.market_replay:
                options["market_replay"] = True
            answer = engine.ask(body.question, application.state.store.history(sid), **options)
            response = ChatResponse(
                session_id=body.session_id, request_id=uuid4().hex, **answer,
            )
            application.state.store.append(sid, body.question, response.model_dump(mode="json"))
            return response
        except HTTPException:
            raise
        except Exception as error:
            logging.getLogger(__name__).error("Demo query failed: %s", type(error).__name__)
            raise HTTPException(503, "本次问答失败，请稍后重试；没有降级返回未校验答案") from None
        finally:
            lock.release()

    @application.get("/documents/{document_id}.pdf", include_in_schema=False)
    def document_pdf(document_id: str):
        document = application.state.documents.get(document_id)
        if document is None:
            raise HTTPException(404, "文档不存在")
        path = (root / document["local_path"]).resolve()
        if not path.is_relative_to(root / "data/raw") or not path.is_file():
            raise HTTPException(404, "文档不可用")
        if file_sha256(path) != document["sha256"]:
            raise HTTPException(409, "文档已变化，暂停引用查看")
        return FileResponse(path, media_type="application/pdf", filename=document_id + ".pdf",
                            content_disposition_type="inline")

    @application.get("/", include_in_schema=False)
    def homepage():
        return FileResponse(STATIC / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC), name="static")
    return application


app = create_app()
