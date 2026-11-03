"""失败草稿仅写本机受控诊断目录，不返回浏览器，不保存API配置和远端错误正文。"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class FailureRecorder:
    def __init__(self, directory: Path, *, secrets: tuple[str, ...] = ()):
        self.directory, self.secrets = directory, secrets

    def record(self, kind: str, data: dict) -> str | None:
        diagnostic_id = uuid4().hex
        payload = {"diagnostic_id": diagnostic_id, "kind": kind,
                   "recorded_at_utc": datetime.now(UTC).isoformat(), **data}
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        for secret in self.secrets:
            if secret:
                encoded = encoded.replace(secret, "[REDACTED]")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / (diagnostic_id + ".json")).open("x", encoding="utf-8") as out:
                out.write(encoded + "\n")
        except OSError as error:
            logging.getLogger(__name__).error("QA diagnostic unavailable: %s", type(error).__name__)
            return None
        return diagnostic_id
