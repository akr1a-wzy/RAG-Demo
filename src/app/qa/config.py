from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class QASettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_read_timeout_seconds: float = Field(default=45, gt=0, le=180)
    deepseek_max_tokens: int = Field(default=1800, ge=64, le=8192)
    qa_session_db: Path = Path("data/qa_sessions.sqlite")

    @field_validator("deepseek_base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("API地址必须是无凭据、无查询参数的HTTP(S)地址")
        return value.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.deepseek_api_key.get_secret_value().strip())
