"""Settings → Notifications, and the per-monitor notification switch."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.security.secrets import SecretBox
from app.services.notify import LAST_ERROR_KEY, LAST_SENT_KEY
from tests.integration.conftest import login, make_client
from tests.integration.test_notify import FakeSender

SECRET_URL = "json://user:hunter2@hooks.example.com/job-alerts"


@pytest.fixture
def ui(password_settings: Settings) -> Iterator[tuple[TestClient, str]]:
    with make_client(password_settings) as client:
        yield client, login(client)


def rows(settings: Settings, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(settings.db_path)
    try:
        result = conn.execute(sql, args).fetchall()
        conn.commit()
        return result
    finally:
        conn.close()


def add(client: TestClient, csrf: str, name: str = "Phone", url: str = SECRET_URL) -> Any:
    return client.post(
        "/settings/notifications", data={"csrf_token": csrf, "name": name, "url": url}
    )


def test_add_target_stores_it_encrypted_and_shows_it_masked(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    assert "No notification targets yet" in client.get("/settings").text

    response = add(client, csrf)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?notify=added#notifications-title"

    ((name, stored),) = rows(
        password_settings, "SELECT name, apprise_url_encrypted FROM notification_targets"
    )
    assert name == "Phone"
    assert "hunter2" not in stored and "hooks.example.com" not in stored
    assert SecretBox(password_settings.secrets_key_path).decrypt(stored) == SECRET_URL
    assert password_settings.secrets_key_path.exists()

    page = client.get("/settings?notify=added").text
    assert "Notification target added" in page
    assert "hooks.example.com/job-alerts" in page
    assert "hunter2" not in page  # never shown again after saving


@pytest.mark.parametrize(
    ("name", "url", "message"),
    [
        ("", SECRET_URL, "Give it a name"),
        ("Phone", "", "Enter a notification URL"),
        ("Phone", "https://example.com/not-apprise-format", "isn&#39;t a notification URL"),
        ("Phone", "nonsense://x", "isn&#39;t a notification URL"),
        ("x" * 101, SECRET_URL, "at most 100 characters"),
    ],
)
def test_add_target_validation(
    ui: tuple[TestClient, str], password_settings: Settings, name: str, url: str, message: str
) -> None:
    client, csrf = ui
    response = add(client, csrf, name, url)
    assert response.status_code == 422
    assert message in response.text
    assert 'aria-invalid="true"' in response.text
    assert rows(password_settings, "SELECT count(*) FROM notification_targets") == [(0,)]


def test_send_test(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    sender = FakeSender()
    client.app.state.notifier.sender = sender  # type: ignore[attr-defined]
    add(client, csrf)
    hx = {"HX-Request": "true", "X-CSRF-Token": csrf}

    result = client.post("/settings/notifications/1/test", headers=hx)
    assert "Test sent" in result.text
    assert sender.sent[0][0] == [SECRET_URL]
    assert sender.sent[0][1].title == "Job Monitor test"

    sender.ok = False
    result = client.post("/settings/notifications/1/test", headers=hx)
    assert "server said no" in result.text
    redirect = client.post("/settings/notifications/1/test", data={"csrf_token": csrf})
    assert redirect.headers["location"] == "/settings?notify=test-failed#notifications-title"
    assert "could not be delivered" in client.get("/settings?notify=test-failed").text


def test_pause_resume_and_remove(ui: tuple[TestClient, str], password_settings: Settings) -> None:
    client, csrf = ui
    add(client, csrf)
    client.post("/settings/notifications/1/toggle", data={"csrf_token": csrf})
    assert rows(password_settings, "SELECT enabled FROM notification_targets") == [(0,)]
    page = client.get("/settings").text
    assert "Paused" in page and "Resume" in page
    client.post("/settings/notifications/1/toggle", data={"csrf_token": csrf})
    assert rows(password_settings, "SELECT enabled FROM notification_targets") == [(1,)]

    client.post("/settings/notifications/1/delete", data={"csrf_token": csrf})
    assert rows(password_settings, "SELECT count(*) FROM notification_targets") == [(0,)]
    assert (
        client.post("/settings/notifications/1/delete", data={"csrf_token": csrf}).status_code
        == 404
    )


def test_target_from_another_install(
    ui: tuple[TestClient, str], password_settings: Settings, tmp_path: Path
) -> None:
    client, csrf = ui
    foreign = SecretBox(tmp_path / "other.key").encrypt(SECRET_URL)
    rows(
        password_settings,
        "INSERT INTO notification_targets (name, apprise_url_encrypted, enabled) VALUES (?, ?, 1)",
        "Old phone",
        foreign,
    )
    page = client.get("/settings").text
    assert "saved by another install" in page
    assert "Send test" not in page
    result = client.post(
        "/settings/notifications/1/test", headers={"HX-Request": "true", "X-CSRF-Token": csrf}
    )
    assert "Remove and re-add it" in result.text


def test_last_failure_shown_until_a_later_success(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, _ = ui
    rows(
        password_settings,
        "INSERT INTO settings (key, value) VALUES (?, ?), (?, ?)",
        LAST_SENT_KEY,
        "2026-10-01T00:00:00+00:00",
        LAST_ERROR_KEY,
        "2026-10-02T00:00:00+00:00 1 target(s) failed (FAILURE).",
    )
    assert "The last notification failed" in client.get("/settings").text
    rows(
        password_settings,
        "UPDATE settings SET value = '2026-10-03T00:00:00+00:00' WHERE key = ?",
        LAST_SENT_KEY,
    )
    page = client.get("/settings").text
    assert "The last notification failed" not in page
    assert "Last notification sent" in page


def test_monitor_notification_switch(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    base = {
        "csrf_token": csrf,
        "url": "https://boards.greenhouse.io/examplecorp",
        "interval_hours": "12",
        "enabled": "on",
        "respect_robots": "on",
        "title_only": "on",
        "render_mode": "auto",
    }
    assert (
        'id="notify" name="notify" type="checkbox" value="on" checked'
        in client.get("/monitors/new").text
    )
    client.post("/monitors", data=base)  # checkbox unticked
    assert rows(password_settings, "SELECT notify FROM monitors") == [(0,)]
    assert "<dd>off</dd>" in client.get("/monitors/1").text
    client.post("/monitors/1", data={**base, "notify": "on"})
    assert rows(password_settings, "SELECT notify FROM monitors") == [(1,)]


def test_notification_routes_need_csrf(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    add(client, csrf)
    for path in ("", "/1/test", "/1/toggle", "/1/delete"):
        assert client.post(f"/settings/notifications{path}").status_code == 403
