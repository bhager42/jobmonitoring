"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DB_FILENAME = "jobmonitor.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    port: int = Field(default=8080, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"

    app_password: SecretStr | None = None
    app_password_file: Path | None = None
    secure_cookies: bool = False
    trust_proxy: bool = False
    forwarded_allow_ips: str = "127.0.0.1"

    max_concurrent_checks: int = Field(default=3, ge=1, le=20)
    default_interval_hours: int = Field(default=12, ge=1, le=720)
    allow_private_urls: bool = False
    backup_keep: int = Field(default=7, ge=1, le=365)

    # Advanced: route outbound fetches through a proxy, trusting an extra CA bundle (for TLS
    # inspecting proxies). With a proxy, only the pre-flight DNS check guards private addresses.
    outbound_proxy: str | None = None
    outbound_ca_bundle: Path | None = None
    # Advanced: use a specific Chromium binary instead of Playwright's bundled one.
    chromium_executable: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


@lru_cache
def get_settings() -> Settings:
    return Settings()
