"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    port: int = Field(default=8080, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"
    trust_proxy: bool = False
    forwarded_allow_ips: str = "127.0.0.1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
