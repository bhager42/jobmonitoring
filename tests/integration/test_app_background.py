"""The real app lifespan starts the scheduler, closes interrupted runs, and backs up daily."""

import sqlite3
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.models import utcnow
from app.main import create_app
from app.services.scheduler import Scheduler


def test_startup_closes_interrupted_runs_and_takes_first_backup(
    password_settings: Settings,
) -> None:
    conn = sqlite3.connect(password_settings.db_path)
    conn.execute(
        "INSERT INTO monitors (name, url, adapter_config, render_mode, criteria, interval_hours,"
        " enabled, respect_robots, notify, consecutive_failures, baseline_done, created_at,"
        " updated_at) VALUES ('Paused', 'https://x.example/', '{}', 'auto', '{}', 12, 0, 1, 1,"
        " 0, 0, '2026-01-01', '2026-01-01')"
    )
    # Recent, so daily maintenance (90-day retention) doesn't prune it straight away.
    started = (utcnow() - timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=" ")
    conn.execute(
        "INSERT INTO check_runs (monitor_id, started_at, status, postings_found, new_postings,"
        " new_matches) VALUES (1, ?, 'running', 0, 0, 0)",
        (started,),
    )
    conn.commit()
    conn.close()

    app = create_app(password_settings, failed_login_delay=0)
    with TestClient(app):
        scheduler: Scheduler = app.state.scheduler
        assert scheduler._loop_task is not None
        for _ in range(200):  # the first tick runs maintenance in the background
            if list(password_settings.backup_dir.glob("daily-*.db")):
                break
            time.sleep(0.02)
        assert list(password_settings.backup_dir.glob("daily-*.db"))
    assert scheduler._loop_task is None  # stopped on shutdown

    conn = sqlite3.connect(password_settings.db_path)
    try:
        row = conn.execute("SELECT status, error FROM check_runs").fetchone()
    finally:
        conn.close()
    assert row is not None
    status, error = row
    assert status == "error"
    assert "Interrupted" in error
