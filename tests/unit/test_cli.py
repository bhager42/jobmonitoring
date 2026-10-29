import os
from pathlib import Path

import pytest

from app.cli import ensure_data_dir_writable


def test_creates_missing_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "data"
    ensure_data_dir_writable(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
def test_exits_with_message_when_not_writable(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(SystemExit, match="not writable"):
            ensure_data_dir_writable(tmp_path)
    finally:
        tmp_path.chmod(0o700)


def test_serve_runs_single_worker_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from app.cli import main
    from app.config import get_settings

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **kw: calls.append(kw))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PORT", "9000")
    get_settings.cache_clear()
    try:
        main(["serve"])
    finally:
        get_settings.cache_clear()

    assert len(calls) == 1
    assert (tmp_path / "jobmonitor.db").exists()  # migrated before starting the server
    assert calls[0]["port"] == 9000
    assert calls[0]["workers"] == 1
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["forwarded_allow_ips"] is None


def test_migrate_command_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.cli import main
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        main(["migrate"])
        assert "Migrated database from empty" in capsys.readouterr().out
        main(["migrate"])
        assert "Database is up to date" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_migrate_command_reports_too_new_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from app.cli import main
    from app.config import get_settings

    with sqlite3.connect(tmp_path / "jobmonitor.db") as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES ('ffffffffffff')")
    conn.close()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="newer image"):
            main(["migrate"])
    finally:
        get_settings.cache_clear()


def test_reset_password_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sqlite3

    from app.auth.passwords import verify_password
    from app.cli import main
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        main(["migrate"])
        with sqlite3.connect(tmp_path / "jobmonitor.db") as conn:
            conn.execute(
                "INSERT INTO sessions (id, created_at, last_seen_at, expires_at) "
                "VALUES ('s', '2026-01-01', '2026-01-01', '2099-01-01')"
            )
        conn.close()
        monkeypatch.setattr("sys.stdin", io.StringIO("a whole new password\n"))
        main(["reset-password", "--password-stdin"])
    finally:
        get_settings.cache_clear()

    conn = sqlite3.connect(tmp_path / "jobmonitor.db")
    (stored,) = conn.execute("SELECT value FROM settings WHERE key = 'password_hash'").fetchone()
    (sessions,) = conn.execute("SELECT count(*) FROM sessions").fetchone()
    conn.close()
    assert verify_password(stored, "a whole new password")
    assert sessions == 0


def test_reset_password_prompts_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.cli import main
    from app.config import get_settings

    answers = iter(["first password!!", "second password!"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="do not match"):
            main(["reset-password"])
    finally:
        get_settings.cache_clear()


def test_probe_prints_detected_postings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import respx

    from app.cli import main
    from app.config import get_settings
    from tests.scraping_helpers import fixture_json

    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "true")  # skip real DNS lookups in this test
    get_settings.cache_clear()
    try:
        with respx.mock() as mock:
            mock.get(url__startswith="https://boards-api.greenhouse.io/").respond(
                200, json=fixture_json("greenhouse_jobs.json")
            )
            main(["probe", "https://boards.greenhouse.io/examplecorp", "--limit", "1"])
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert 'Adapter:   greenhouse {"token": "examplecorp"}' in out
    assert "Postings:  2" in out
    assert "1. Senior Backend Engineer (Python)  [Remote - US · Engineering · remote" in out
    assert "... and 1 more" in out


def test_probe_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli import main
    from app.config import get_settings

    monkeypatch.delenv("ALLOW_PRIVATE_URLS", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="non-public"):
            main(["probe", "http://127.0.0.1/jobs", "--render", "static"])
    finally:
        get_settings.cache_clear()
