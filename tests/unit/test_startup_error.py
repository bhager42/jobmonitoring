from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cli import data_dir_problem, main, startup_problem
from app.config import Settings, get_settings
from app.startup_error import startup_error_app


def test_error_app_explains_and_fails_health() -> None:
    client = TestClient(startup_error_app("data directory /data is not <writable>"))
    health = client.get("/healthz")
    assert health.status_code == 503
    assert health.json() == {"status": "error", "error": "data directory /data is not <writable>"}
    page = client.get("/monitors/1")
    assert page.status_code == 503
    assert "Job Monitor can&#x27;t start" in page.text or "Job Monitor can't start" in page.text
    assert "is not &lt;writable&gt;" in page.text  # escaped
    assert page.headers["content-security-policy"].startswith("default-src 'self'")


def test_data_dir_problem(tmp_path: Path) -> None:
    assert data_dir_problem(tmp_path / "data") is None
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    problem = data_dir_problem(not_a_dir)
    assert problem is not None and "is not writable" in problem and "named volume" in problem


def settings_for(tmp_path: Path, **values: Any) -> Settings:
    return Settings(data_dir=tmp_path / "data", **values)


def test_startup_problem_none_when_healthy(tmp_path: Path) -> None:
    assert startup_problem(settings_for(tmp_path)) is None
    assert (tmp_path / "data" / "jobmonitor.db").exists()  # migrated as part of the check


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"app_password": "short"}, "APP_PASSWORD is not acceptable"),
        ({"app_password_file": Path("/nonexistent/password")}, "Cannot read APP_PASSWORD_FILE"),
        (
            {"app_password": "long enough password", "app_password_file": Path("/x")},
            "only one of",
        ),
    ],
)
def test_startup_problem_reports_bad_password_config(
    tmp_path: Path, values: dict[str, Any], message: str
) -> None:
    problem = startup_problem(settings_for(tmp_path, **values))
    assert problem is not None and message in problem


def test_short_env_password_is_ignored_once_a_password_exists(tmp_path: Path) -> None:
    import sqlite3

    settings = settings_for(tmp_path)
    assert startup_problem(settings) is None
    conn = sqlite3.connect(settings.db_path)
    conn.execute("INSERT INTO settings (key, value) VALUES ('password_hash', 'x')")
    conn.commit()
    conn.close()
    assert startup_problem(settings_for(tmp_path, app_password="short")) is None


def test_serve_runs_the_error_app_instead_of_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    blocked = tmp_path / "file"
    blocked.write_text("x")
    monkeypatch.setenv("DATA_DIR", str(blocked))
    get_settings.cache_clear()
    try:
        main(["serve"])  # must not raise SystemExit
    finally:
        get_settings.cache_clear()
    ((app, kwargs),) = calls
    assert not isinstance(app, str)  # the error app, not the real one
    assert kwargs["port"] == 8080
    response = TestClient(app).get("/healthz")
    assert response.status_code == 503
    assert "is not writable" in response.json()["error"]
