from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """集中管理可复现运行所需的配置。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    tencent_connect_timeout_seconds: float = 5.0
    tencent_read_timeout_seconds: float = 10.0
    tencent_max_attempts: int = 3
    chroma_dir: Path = Path("data/chroma")
    checkpoint_db: Path = Path("data/checkpoints.sqlite")
    cache_dir: Path = Path("data/cache")
    embedding_model: str = "BAAI/bge-small-zh-v1.5"


settings = Settings()
