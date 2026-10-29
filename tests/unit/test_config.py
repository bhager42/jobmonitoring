from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PORT", "DATA_DIR", "LOG_LEVEL", "TRUST_PROXY"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert settings.port == 8080
    assert settings.data_dir == Path("/data")
    assert settings.trust_proxy is False


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUST_PROXY", "true")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    settings = Settings()
    assert settings.trust_proxy is True
    assert settings.log_level == "debug"


@pytest.mark.parametrize(("var", "value"), [("PORT", "0"), ("LOG_LEVEL", "loud")])
def test_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError):
        Settings()
