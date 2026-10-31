"""Route-level tests for the Phase 5 web UI (the browser journey is in tests/e2e)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.checks import CheckResult
from app.web.assets import IMMUTABLE, static_url
from tests.integration.conftest import csrf_from, login, make_client
from tests.scraping_helpers import fixture_json, fixture_text
from tests.scraping_helpers import make_client as make_http_client

BOARD = "https://boards.greenhouse.io/examplecorp"
GH_API = "https://boards-api.greenhouse.io/"


@pytest.fixture
def ui(password_settings: Settings) -> Iterator[tuple[TestClient, str]]:
    """A signed-in client whose outbound fetches use offline DNS and never run real checks."""
    with make_client(password_settings) as client:
        app: Any = client.app
        app.state.http_client = make_http_client()
        app.state.browser = None
        calls: list[int] = []

        async def fake_run(monitor_id: int) -> CheckResult:
            calls.append(monitor_id)
            return CheckResult(monitor_id, "ok")

        app.state.runner.run = fake_run
        app.state.check_calls = calls
        yield client, login(client)


def form(csrf: str, **values: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "csrf_token": csrf,
        "url": BOARD,
        "interval_hours": "12",
        "enabled": "on",
        "respect_robots": "on",
        "title_only": "on",
        "notify": "on",
        "render_mode": "auto",
    }
    data.update(values)
    return {k: v for k, v in data.items() if v is not None}


def db(settings: Settings, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def create(client: TestClient, csrf: str, **values: Any) -> int:
    response = client.post("/monitors", data=form(csrf, **values))
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[2].split("?")[0])


# --- static assets ---------------------------------------------------------------------------


def test_versioned_static_assets_are_immutable(ui: tuple[TestClient, str]) -> None:
    client, _ = ui
    url = static_url("css/app.css")
    assert "?v=" in url
    assert client.get(url).headers["cache-control"] == IMMUTABLE
    assert client.get("/static/css/app.css").headers["cache-control"] == "no-cache"
    assert client.get("/static/css/app.css?v=stale").headers["cache-control"] == "no-cache"
    page = client.get("/").text
    assert url in page
    assert static_url("js/htmx.min.js") in page


def test_manifest_and_service_worker(setup_client: TestClient) -> None:
    manifest = setup_client.get(static_url("manifest.webmanifest"))
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["display"] == "standalone"
    for icon in manifest.json()["icons"]:
        assert setup_client.get(icon["src"]).status_code == 200
    # Public even before a password exists (the setup page registers it).
    worker = setup_client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.headers["content-type"].startswith("text/javascript")
    assert worker.headers["cache-control"] == "no-cache"


def test_unknown_static_file_is_an_error() -> None:
    with pytest.raises(ValueError):
        static_url("nope.css")


# --- create / edit ---------------------------------------------------------------------------


def test_create_validation_errors(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    response = client.post(
        "/monitors",
        data=form(
            csrf,
            url="ftp://example.com",
            interval_hours="0",
            keywords_any="x" * 101,
            title_regex="([bad",
        ),
    )
    assert response.status_code == 422
    html = response.text
    assert "Please fix these 4 things" in html
    assert "starting with https://" in html
    assert "1 to 720 hours" in html
    assert "longer than 100 characters" in html
    assert "not a valid regular expression" in html
    assert 'aria-invalid="true"' in html
    assert 'value="ftp://example.com"' in html  # input kept


def test_detect_then_create_uses_the_preview(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    with respx.mock() as mock:
        mock.get(url__startswith=GH_API).respond(200, json=fixture_json("greenhouse_jobs.json"))
        html = client.post(
            "/monitors/detect",
            data=form(csrf, keywords_any="engineer"),
            headers={"HX-Request": "true"},
        ).text
    assert "Found <strong>2</strong> postings" in html
    assert "1</strong> of the 2 postings match" in html
    assert "title contains &#39;engineer&#39;" in html
    token = html.split('name="preview_token" value="')[1].split('"')[0]
    assert token

    monitor_id = create(client, csrf, preview_token=token, name="Example", keywords_any="engineer")
    adapter, config, baseline, next_check = db(
        password_settings,
        "SELECT adapter, adapter_config, baseline_done, next_check_at FROM monitors WHERE id = ?",
        monitor_id,
    )[0]
    assert (adapter, config, baseline, next_check) == (
        "greenhouse",
        '{"token": "examplecorp"}',
        0,
        None,
    )


def test_detect_without_htmx_renders_the_whole_form(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    with respx.mock() as mock:
        mock.get(url__startswith=GH_API).respond(200, json=fixture_json("greenhouse_jobs.json"))
        response = client.post("/monitors/detect", data=form(csrf))
    assert response.status_code == 200
    assert "<h1>Add a monitor</h1>" in response.text
    assert "Found <strong>2</strong> postings" in response.text


def test_detect_reports_problems(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    response = client.post(
        "/monitors/detect",
        data=form(csrf, url="http://192.168.1.1/careers"),
        headers={"HX-Request": "true"},
    )
    assert "non-public address" in response.text
    response = client.post(
        "/monitors/detect", data=form(csrf, url="not a url"), headers={"HX-Request": "true"}
    )
    assert "starting with https://" in response.text


def test_live_match_preview(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    with respx.mock() as mock:
        mock.get("https://shop.example.com/robots.txt").respond(404)
        mock.get("https://shop.example.com/jobs").respond(200, html=fixture_text("css_page.html"))
        detected = client.post(
            "/monitors/detect",
            data=form(
                csrf,
                url="https://shop.example.com/jobs",
                item_selector="li.opening",
                title_selector=".role",
            ),
            headers={"HX-Request": "true"},
        ).text
    token = detected.split('name="preview_token" value="')[1].split('"')[0]
    preview = client.post(
        "/monitors/preview",
        data=form(csrf, preview_token=token, keywords_any="barista"),
        headers={"HX-Request": "true"},
    ).text
    assert "1</strong> of the 2 postings match" in preview
    bad = client.post(
        "/monitors/preview",
        data=form(csrf, preview_token=token, title_regex="(["),
        headers={"HX-Request": "true"},
    ).text
    assert "Fix the criteria to see matches" in bad
    expired = client.post(
        "/monitors/preview", data=form(csrf, preview_token="gone"), headers={"HX-Request": "true"}
    ).text
    assert "postings match" not in expired


def test_create_without_preview_detects_on_first_check(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf, name="")
    name, adapter = db(
        password_settings, "SELECT name, adapter FROM monitors WHERE id = ?", monitor_id
    )[0]
    assert (name, adapter) == ("boards.greenhouse.io", None)  # name defaults to the host


def test_css_selectors_without_preview(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf, url="https://shop.example.com/jobs", item_selector="li.job")
    adapter, config = db(
        password_settings, "SELECT adapter, adapter_config FROM monitors WHERE id = ?", monitor_id
    )[0]
    assert (adapter, config) == ("css_selector", '{"item_selector": "li.job"}')


def test_editing_the_url_restarts_the_baseline(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf, keywords_any="engineer")
    db(
        password_settings,
        "UPDATE monitors SET adapter='greenhouse', adapter_config='{\"token\": \"examplecorp\"}',"
        " baseline_done=1, next_check_at='2030-01-01 00:00:00' WHERE id = ?",
        monitor_id,
    )
    edit = client.get(f"/monitors/{monitor_id}/edit").text
    assert 'value="engineer"' in edit

    # Changing only the criteria keeps the detected adapter and the baseline.
    client.post(f"/monitors/{monitor_id}", data=form(csrf, keywords_any="designer"))
    assert db(
        password_settings, "SELECT adapter, baseline_done FROM monitors WHERE id = ?", monitor_id
    ) == [("greenhouse", 1)]

    # Changing the URL forgets the adapter and starts over, so nothing old alerts.
    client.post(
        f"/monitors/{monitor_id}", data=form(csrf, url="https://boards.greenhouse.io/othercorp")
    )
    assert db(
        password_settings,
        "SELECT adapter, baseline_done, next_check_at FROM monitors WHERE id = ?",
        monitor_id,
    ) == [(None, 0, None)]


def test_editing_criteria_rejudges_the_listed_postings(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    """Adding a filter and removing it again brings the earlier matches back."""
    client, csrf = ui
    monitor_id = create(client, csrf)
    db(
        password_settings,
        "UPDATE monitors SET adapter='greenhouse', adapter_config='{\"token\": \"examplecorp\"}',"
        " baseline_done=1 WHERE id = ?",
        monitor_id,
    )
    jobs = [
        ("Backend Engineer", "Durham, NC", 1),
        ("Data Engineer", "Austin, TX", 1),
        ("Office Manager", "Durham, NC", 1),
        ("Old Engineer role", "Durham, NC", 0),  # no longer listed: never re-judged
    ]
    for n, (title, location, active) in enumerate(jobs):
        db(
            password_settings,
            "INSERT INTO postings (monitor_id, fingerprint, title, url, location, first_seen_at,"
            " last_seen_at, missing_count, is_active)"
            " VALUES (?, ?, ?, ?, ?, '2026-10-01', '2026-10-01', 0, ?)",
            monitor_id, f"{n:064d}", title, f"https://acme.example/jobs/{n}", location, active,
        )  # fmt: skip

    def matched() -> list[str]:
        rows = db(
            password_settings,
            "SELECT p.title FROM matches m JOIN postings p ON p.id = m.posting_id"
            " WHERE m.monitor_id = ? ORDER BY p.id",
            monitor_id,
        )
        return [title for (title,) in rows]

    # The edit page previews the criteria against what the last check found.
    edit = client.get(f"/monitors/{monitor_id}/edit").text
    assert "the last check found" in edit
    assert "Office Manager" in edit

    response = client.post(f"/monitors/{monitor_id}", data=form(csrf, keywords_any="engineer"))
    assert response.headers["location"].endswith("saved=true&added=2&removed=0")
    assert matched() == ["Backend Engineer", "Data Engineer"]
    page = client.get(response.headers["location"]).text
    assert "2 new matches, 0 removed" in page
    assert "not sent as notifications" in page

    client.post(
        f"/monitors/{monitor_id}", data=form(csrf, keywords_any="engineer", locations_any="durham")
    )
    assert matched() == ["Backend Engineer"]

    # Dismissed matches stay dismissed when a filter is loosened again.
    db(password_settings, "UPDATE matches SET dismissed = 1 WHERE monitor_id = ?", monitor_id)
    response = client.post(f"/monitors/{monitor_id}", data=form(csrf, keywords_any="engineer"))
    assert response.headers["location"].endswith("added=1&removed=0")
    assert matched() == ["Backend Engineer", "Data Engineer"]

    # Saving without changing the criteria leaves the matches alone.
    response = client.post(f"/monitors/{monitor_id}", data=form(csrf, keywords_any="engineer"))
    assert response.headers["location"].endswith("?saved=true")
    assert "the matches didn't change" not in client.get(response.headers["location"]).text

    # Nothing is sent for matches found by an edit.
    assert db(password_settings, "SELECT count(*) FROM matches WHERE notified_at IS NOT NULL") == [
        (0,)
    ]


def test_shorter_interval_brings_the_next_check_forward(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf, interval_hours="24")
    last = datetime(2030, 1, 1, tzinfo=UTC)
    db(
        password_settings,
        "UPDATE monitors SET last_check_at=?, next_check_at=? WHERE id = ?",
        last.replace(tzinfo=None).isoformat(sep=" "),
        (last + timedelta(hours=24)).replace(tzinfo=None).isoformat(sep=" "),
        monitor_id,
    )
    client.post(f"/monitors/{monitor_id}", data=form(csrf, interval_hours="6"))
    (next_check,) = db(
        password_settings, "SELECT next_check_at FROM monitors WHERE id = ?", monitor_id
    )[0]
    assert datetime.fromisoformat(next_check) == datetime(2030, 1, 1, 6, tzinfo=UTC).replace(
        tzinfo=None
    )


# --- detail and actions ----------------------------------------------------------------------


def test_detail_check_now_status_and_toggle(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf, name="Example Corp")
    page = client.get(f"/monitors/{monitor_id}")
    assert "Example Corp" in page.text
    assert "Nothing recorded yet" in page.text

    status = client.post(
        f"/monitors/{monitor_id}/check", headers={"HX-Request": "true", "X-CSRF-Token": csrf}
    )
    assert f'id="status-{monitor_id}"' in status.text
    assert "Checking now" in status.text
    assert 'hx-trigger="every 3s"' in status.text

    # The fake check has finished; the poll asks the page to reload.
    done = client.get(f"/monitors/{monitor_id}/status?was_busy=true")
    assert done.headers.get("hx-refresh") == "true"
    assert "every 3s" not in done.text
    assert "hx-refresh" not in client.get(f"/monitors/{monitor_id}/status").headers

    without_js = client.post(f"/monitors/{monitor_id}/check", data={"csrf_token": csrf})
    assert without_js.headers["location"] == f"/monitors/{monitor_id}"

    toggled = client.post(
        f"/monitors/{monitor_id}/toggle", headers={"HX-Request": "true", "X-CSRF-Token": csrf}
    )
    assert 'aria-checked="false"' in toggled.text
    assert "Paused" in client.get("/monitors").text


def test_delete_with_and_without_javascript(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf, name="Doomed")
    detail = client.get(f"/monitors/{monitor_id}").text
    assert '<dialog id="delete-dialog"' in detail
    assert f'href="/monitors/{monitor_id}/delete" data-dialog-open="delete-dialog"' in detail
    assert "It can't be undone" in client.get(f"/monitors/{monitor_id}/delete").text
    response = client.post(f"/monitors/{monitor_id}/delete", data={"csrf_token": csrf})
    assert response.headers["location"] == "/monitors?deleted=true"
    assert db(password_settings, "SELECT count(*) FROM monitors") == [(0,)]
    missing = client.get(f"/monitors/{monitor_id}")
    assert missing.status_code == 404
    assert '<h1 id="page-title">Not found</h1>' in missing.text
    assert client.get("/api/v1/nope").json() == {"detail": "Not Found"}


# --- matches ---------------------------------------------------------------------------------


def seed_matches(settings: Settings, count: int) -> int:
    db(
        settings,
        "INSERT INTO monitors (name, url, adapter_config, render_mode, criteria,"
        " interval_hours, enabled, respect_robots, notify, consecutive_failures,"
        " baseline_done, alert_existing, created_at, updated_at, last_status, last_error)"
        " VALUES ('Acme', 'https://acme.example/', '{}', 'auto', '{}', 12, 1, 1, 1, 0, 1, 0,"
        " '2026-01-01', '2026-01-01', 'error', 'HTTP 500')",
    )
    (monitor_id,) = db(settings, "SELECT max(id) FROM monitors")[0]
    for n in range(count):
        db(
            settings,
            "INSERT INTO postings (monitor_id, fingerprint, title, url, first_seen_at,"
            " last_seen_at, missing_count, is_active)"
            " VALUES (?, ?, ?, ?, '2026-10-01', '2026-10-01', 0, 1)",
            monitor_id,
            f"{n:064d}",
            f"Engineer {n}",
            f"https://acme.example/jobs/{n}",
        )
        db(
            settings,
            "INSERT INTO matches (monitor_id, posting_id, reasons, matched_at, seen, dismissed)"
            " VALUES (?, (SELECT max(id) FROM postings), '[\"title contains engineer\"]',"
            " ?, 0, 0)",
            monitor_id,
            f"2026-10-0{1 + n % 9} 00:00:00",
        )
    return monitor_id


def test_dashboard_lists_new_matches_and_problems(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, _ = ui
    seed_matches(password_settings, 3)
    html = client.get("/").text
    assert html.count('class="match-card') == 3
    assert ">3</span>" in html and "new matches" in html
    assert "Needs attention" in html and "HTTP 500" in html


def test_match_actions(ui: tuple[TestClient, str], password_settings: Settings) -> None:
    client, csrf = ui
    seed_matches(password_settings, 2)
    hx = {"HX-Request": "true", "X-CSRF-Token": csrf}

    gone = client.post("/matches/1/seen", data={"context": "dashboard"}, headers=hx)
    assert gone.text == ""
    assert gone.headers["hx-trigger"] == "matches-changed"
    assert ">1</span>" in client.get("/matches/count").text

    card = client.post("/matches/2/dismiss", data={"context": "list"}, headers=hx)
    assert "is-dismissed" in card.text and "Restore" in card.text
    card = client.post("/matches/2/restore", data={"context": "list"}, headers=hx)
    assert "Dismiss" in card.text
    card = client.post("/matches/1/unseen", data={"context": "list"}, headers=hx)
    assert "Mark seen" in card.text

    redirect = client.post("/matches/1/seen", data={"csrf_token": csrf, "next": "//evil.example"})
    assert redirect.headers["location"] == "/"
    assert client.post("/matches/999/seen", headers=hx).status_code == 404

    client.post("/matches/seen-all", data={"csrf_token": csrf})
    assert ">0</span>" in client.get("/matches/count").text


def test_matches_filters_and_pages(ui: tuple[TestClient, str], password_settings: Settings) -> None:
    client, _ = ui
    monitor_id = seed_matches(password_settings, 30)
    first = client.get("/matches").text
    assert first.count('class="match-card') == 25
    assert "Page 1 of 2" in first
    second = client.get(f"/matches?state=unseen&monitor={monitor_id}&page=2").text
    assert second.count('class="match-card') == 5
    assert client.get("/matches?state=dismissed").text.count('class="match-card') == 0
    assert client.get("/matches?state=bogus").status_code == 422


# --- settings --------------------------------------------------------------------------------


def test_settings_backup_download(ui: tuple[TestClient, str], tmp_path: Any) -> None:
    client, _ = ui
    page = client.get("/settings").text
    assert "Download a backup now" in page
    assert "Not in this image" in page  # browser is None in this fixture
    response = client.get("/settings/backup")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    copy = tmp_path / "copy.db"
    copy.write_bytes(response.content)
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] >= 2
    conn.close()


def test_new_routes_need_csrf(ui: tuple[TestClient, str]) -> None:
    client, csrf = ui
    monitor_id = create(client, csrf)
    for path in (
        f"/monitors/{monitor_id}/check",
        f"/monitors/{monitor_id}/delete",
        "/matches/seen-all",
    ):
        assert client.post(path).status_code == 403
    assert csrf_from(client.get("/").text) == csrf


def test_htmx_config_keeps_pages_out_of_browser_storage(ui: tuple[TestClient, str]) -> None:
    import html
    import json
    import re

    client, _ = ui
    page = client.get("/").text
    raw = re.search(r'<meta name="htmx-config" content=\'([^\']+)\'', page)
    assert raw is not None
    config = json.loads(html.unescape(raw.group(1)))
    assert config["historyCacheSize"] == 0  # no copies of private pages in localStorage
    assert config["allowEval"] is False  # required by the CSP
    assert config["includeIndicatorStyles"] is False  # required by the CSP
    assert config["selfRequestsOnly"] is True


def test_preview_for_another_url_is_not_reused(
    ui: tuple[TestClient, str], password_settings: Settings
) -> None:
    client, csrf = ui
    with respx.mock() as mock:
        mock.get(url__startswith=GH_API).respond(200, json=fixture_json("greenhouse_jobs.json"))
        html = client.post("/monitors/detect", data=form(csrf), headers={"HX-Request": "true"}).text
    token = html.split('name="preview_token" value="')[1].split('"')[0]
    monitor_id = create(
        client, csrf, preview_token=token, url="https://boards.greenhouse.io/othercorp"
    )
    assert db(password_settings, "SELECT adapter FROM monitors WHERE id = ?", monitor_id) == [
        (None,)
    ]
