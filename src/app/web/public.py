"""Opt-in authenticated public entry; keeps the original local API unchanged."""

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

VERSION = "ngrok-public-entry-v1"


@dataclass(frozen=True)
class PublicConfig:
    origin: str
    username: str
    password_sha256: str
    state_dir: Path
    daily_chat_limit: int = 50
    writes_per_minute: int = 20
    max_body_bytes: int = 16384

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or "*" in parsed.netloc
                or parsed.netloc != parsed.netloc.lower()):
            raise ValueError("Public origin must be an exact HTTPS origin without a path")
        if (not self.username or ":" in self.username or len(self.password_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.password_sha256)):
            raise ValueError("Invalid public authentication configuration")
        if min(self.daily_chat_limit, self.writes_per_minute, self.max_body_bytes) < 1:
            raise ValueError("Public limits must be positive")


class PublicBudget:
    """Persistent conservative request ceiling, NOT a measured model-call counter."""

    def __init__(self, config: PublicConfig):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = config.state_dir / "budget.sqlite"
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests"
                       "(ts REAL NOT NULL, chat INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts)")

    def reserve(self, *, chat: bool, now: float | None = None):
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, UTC) + timedelta(hours=8)
        start = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(hours=8)).timestamp()
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            recent = db.execute("SELECT COUNT(*) FROM requests WHERE ts>?",
                                (now - 60,)).fetchone()[0]
            if recent >= self.config.writes_per_minute:
                return "请求过于频繁，请稍后再试。", 60
            daily = db.execute("SELECT COUNT(*) FROM requests WHERE ts>=? AND chat=1",
                               (start,)).fetchone()[0]
            if chat and daily >= self.config.daily_chat_limit:
                return "今日演示问答额度已用完，请明日再试。", max(1, int(start + 86400 - now))
            db.execute("INSERT INTO requests VALUES(?,?)", (now, int(chat)))
            db.execute("DELETE FROM requests WHERE ts<?", (min(start, now - 60) - 86400,))
        return None


class PublicEntry:
    def __init__(self, app, config: PublicConfig):
        self.app, self.config = app, config
        self.authority = urlsplit(config.origin).netloc.encode("ascii")
        self.budget = PublicBudget(config)
        self.failures = deque()
        self.failure_lock = threading.Lock()

    def authorized(self, raw: bytes) -> bool:
        try:
            scheme, value = raw.split(b" ", 1)
            if scheme.lower() != b"basic":
                return False
            username, password = base64.b64decode(value, validate=True).split(b":", 1)
        except (ValueError, TypeError):
            return False
        return (hmac.compare_digest(username, self.config.username.encode())
                and hmac.compare_digest(hashlib.sha256(password).hexdigest(),
                                        self.config.password_sha256))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"cache-control", b"x-robots-tag"}]
                headers = [(k, self.config.origin.encode() + v[len(b"http://127.0.0.1"):]
                            if k.lower() == b"location" and v.startswith(b"http://127.0.0.1/")
                            else v) for k, v in headers]
                message["headers"] = headers + [
                    (b"cache-control", b"private, no-store"),
                    (b"x-robots-tag", b"noindex, nofollow"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        async def reject(status, detail, headers=None):
            await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
                scope, receive, secure_send)

        # Do not trust X-Forwarded-Host/Origin supplied by the caller.
        pairs = scope.get("headers", [])
        headers = {key.lower(): value for key, value in pairs}
        if sum(key.lower() == b"host" for key, _ in pairs) != 1:
            return await reject(400, "Invalid host")
        if headers.get(b"host", b"").lower() != self.authority:
            return await reject(400, "Invalid host")
        if not self.authorized(headers.get(b"authorization", b"")):
            with self.failure_lock:
                now = time.monotonic()
                while self.failures and self.failures[0] <= now - 60:
                    self.failures.popleft()
                throttled = len(self.failures) >= 10
                if not throttled:
                    self.failures.append(now)
            if throttled:
                return await reject(429, "登录尝试过于频繁。", {"Retry-After": "60"})
            return await reject(401, "请输入演示账号与访问码。", {
                "WWW-Authenticate": 'Basic realm="RAG portfolio demo", charset="UTF-8"',
            })
        method, path = scope["method"], scope["path"]
        writing = method in {"POST", "PUT", "PATCH", "DELETE"}
        if writing and b"origin" in headers:
            origins = [v for k, v in pairs if k.lower() == b"origin"]
            if origins != [self.config.origin.encode()]:
                return await reject(403, "仅允许演示站点同源请求。")
        if path in {"/docs", "/redoc", "/openapi.json"}:
            return await reject(404, "Not found")

        if writing:
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                return await reject(400, "Invalid content length")
            if length < 0 or length > self.config.max_body_bytes:
                return await reject(413, "请求内容过长。")
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > self.config.max_body_bytes:
                    return await reject(413, "请求内容过长。")
                if not event.get("more_body", False):
                    break
            sent_body = False
            original_receive = receive

            async def buffered_receive():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original_receive()

            receive = buffered_receive
            if path.startswith("/api/"):
                limited = self.budget.reserve(
                    chat=method == "POST" and path.rstrip("/") == "/api/chat")
                if limited:
                    return await reject(429, limited[0], {"Retry-After": str(limited[1])})

        # External authority and Origin were verified above. Normalize only the private
        # in-process request so the unchanged local API can retain its own host/CSRF guard.
        inner = dict(scope)
        inner["scheme"], inner["server"] = "http", ("127.0.0.1", 80)
        inner["headers"] = [(k, v) for k, v in pairs if k.lower() not in {
            b"host", b"origin", b"authorization", b"forwarded", b"x-forwarded-host",
            b"x-forwarded-proto", b"x-forwarded-for",
        }] + [(b"host", b"127.0.0.1")]
        if b"origin" in headers:
            inner["headers"].append((b"origin", b"http://127.0.0.1"))
        await self.app(inner, receive, secure_send)


def load_config(path: Path, root: Path) -> PublicConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    state_dir = (root / data.pop("state_dir")).resolve()
    if not state_dir.is_relative_to((root / "data/public_demo").resolve()):
        raise ValueError("Public state must stay inside data/public_demo")
    return PublicConfig(**data, state_dir=state_dir)


def create_public_app(root: Path, config: PublicConfig, *, engine_factory=None):
    from app.qa.config import QASettings
    from app.web.api import create_app

    settings = QASettings(_env_file=root / ".env",
                          qa_session_db=config.state_dir / "sessions.sqlite")
    app = create_app(root, settings=settings, engine_factory=engine_factory)
    return PublicEntry(app, config)
