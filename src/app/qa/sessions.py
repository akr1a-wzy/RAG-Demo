"""本机会话存储；完成一轮后同时保存问题与结果，不持久化API配置。"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turns(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                  question TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  FOREIGN KEY(session_id) REFERENCES sessions(id));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def create(self) -> str:
        session_id = str(uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?, ?)",
                       (session_id, datetime.now(UTC).isoformat()))
        return session_id

    def ensure(self, session_id: str) -> None:
        UUID(session_id)
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("会话不存在，请新建会话")

    def history(self, session_id: str) -> list[dict]:
        self.ensure(session_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT question FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 20",
                (session_id,),
            ).fetchall()
        # 实体/年份只继承历史用户问题，不把生成草稿当检索事实。
        return [{"role": "user", "content": row[0]} for row in reversed(rows)]

    def append(self, session_id: str, question: str, result: dict) -> None:
        self.ensure(session_id)
        with self.connect() as db:
            db.execute(
                "INSERT INTO turns(session_id,question,result_json,created_at) VALUES(?,?,?,?)",
                       (session_id, question, json.dumps(result, ensure_ascii=False),
                        datetime.now(UTC).isoformat()))

    def reset(self, session_id: str) -> None:
        self.ensure(session_id)
        with self.connect() as db:
            db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
