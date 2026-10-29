import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth.bootstrap import AuthState, BootstrapError
from app.auth.ratelimit import MAX_FAILURES
from app.config import Settings
from app.db.models import utcnow
from tests.integration.conftest import PASSWORD, login, make_client


def _db(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.db_path)


def _count(settings: Settings, table: str) -> int:
    conn = _db(settings)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        conn.close()


def _shift_times(settings: Settings, table: str, column: str, delta: timedelta) -> None:
    conn = _db(settings)
    try:
        rows = conn.execute(f"SELECT rowid, {column} FROM {table}").fetchall()  # noqa: S608
        for rowid, value in rows:
            shifted = (datetime.fromisoformat(value) + delta).isoformat(sep=" ")
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (shifted, rowid))  # noqa: S608
        conn.commit()
    finally:
        conn.close()


# --- first-run setup ---------------------------------------------------------------------------


def _setup_token(client: TestClient) -> str:
    auth: AuthState = client.app.state.auth  # type: ignore[attr-defined]
    assert auth.setup_token is not None
    return auth.setup_token


def test_fresh_install_redirects_everything_to_setup(setup_client: TestClient) -> None:
    for path in ("/", "/login", "/settings"):
        response = setup_client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
    assert setup_client.get("/api/v1/session").status_code == 401
    assert setup_client.get("/healthz").status_code == 200
    assert setup_client.get("/setup").status_code == 200


def test_setup_token_is_logged(
    migrated_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with make_client(migrated_settings) as client:
        token = _setup_token(client)
    assert f"Setup token: {token}" in caplog.text


def test_setup_flow(setup_client: TestClient) -> None:
    token = _setup_token(setup_client)
    bad = {"token": "wrong", "password": PASSWORD, "confirm": PASSWORD}
    assert setup_client.post("/setup", data=bad).status_code == 403

    short = {"token": token, "password": "short", "confirm": "short"}
    assert setup_client.post("/setup", data=short).status_code == 422

    mismatch = {"token": token, "password": PASSWORD, "confirm": PASSWORD + "x"}
    response = setup_client.post("/setup", data=mismatch)
    assert response.status_code == 422
    assert "do not match" in response.text

    good = {"token": token, "password": PASSWORD, "confirm": PASSWORD}
    response = setup_client.post("/setup", data=good)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert setup_client.get("/").status_code == 200  # signed in by setup

    # The setup route is gone once a password exists.
    assert setup_client.get("/setup").status_code == 404
    assert setup_client.post("/setup", data=good).status_code == 404


def test_setup_closes_when_password_set_elsewhere(
    setup_client: TestClient, migrated_settings: Settings
) -> None:
    from app.auth.passwords import hash_password

    conn = _db(migrated_settings)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('password_hash', ?)", (hash_password(PASSWORD),)
    )
    conn.commit()
    conn.close()

    assert setup_client.get("/setup").status_code == 404
    assert setup_client.get("/").headers["location"] == "/login"


def test_password_from_env_skips_setup(client: TestClient) -> None:
    assert client.get("/setup").status_code == 404
    assert client.get("/").headers["location"] == "/login"
    login(client)


def test_password_from_file(migrated_settings: Settings, tmp_path: Path) -> None:
    secret = tmp_path / "password.txt"
    secret.write_text(PASSWORD + "\n")
    settings = migrated_settings.model_copy(update={"app_password_file": secret})
    with make_client(settings) as client:
        login(client)


def test_short_env_password_fails_startup(migrated_settings: Settings) -> None:
    settings = migrated_settings.model_copy(update={"app_password": SecretStr("short")})
    with pytest.raises(BootstrapError, match="at least 12"), make_client(settings):
        pass


def test_env_password_ignored_once_set(password_settings: Settings) -> None:
    with make_client(password_settings):
        pass
    changed = password_settings.model_copy(update={"app_password": SecretStr("another password!")})
    with make_client(changed) as client:
        assert client.post("/login", data={"password": "another password!"}).status_code == 401
        login(client)


def test_secret_key_persists_across_restarts(password_settings: Settings) -> None:
    with make_client(password_settings) as first:
        key = first.app.state.auth.secret_key  # type: ignore[attr-defined]
    with make_client(password_settings) as second:
        assert second.app.state.auth.secret_key == key  # type: ignore[attr-defined]


# --- login, protected pages, logout ------------------------------------------------------------


def test_login_protected_page_logout(client: TestClient, password_settings: Settings) -> None:
    assert client.get("/").headers["location"] == "/login"

    wrong = client.post("/login", data={"password": "nope nope nope"})
    assert wrong.status_code == 401
    assert "Incorrect password" in wrong.text

    csrf = login(client)
    assert client.get("/settings").status_code == 200
    assert client.get("/api/v1/session").json() == {"authenticated": True, "csrf_token": csrf}
    assert _count(password_settings, "sessions") == 1

    response = client.post("/logout", data={"csrf_token": csrf})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "jm_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert _count(password_settings, "sessions") == 0
    assert client.get("/").headers["location"] == "/login"


def test_login_page_redirects_when_signed_in(client: TestClient) -> None:
    login(client)
    response = client.get("/login?next=/settings")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_unauthenticated_responses(client: TestClient) -> None:
    page = client.get("/settings?tab=1")
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=%2Fsettings%3Ftab%3D1"

    api = client.get("/api/v1/session")
    assert api.status_code == 401
    assert api.json() == {"detail": "Not authenticated"}

    htmx = client.get("/settings", headers={"HX-Request": "true"})
    assert htmx.status_code == 401
    assert htmx.headers["hx-redirect"].startswith("/login")


@pytest.mark.parametrize(
    ("next_url", "expected"),
    [
        ("/settings", "/settings"),
        ("//evil.example", "/"),
        ("https://evil.example/", "/"),
        ("/\\evil.example", "/"),
    ],
)
def test_login_next_redirect_is_safe(client: TestClient, next_url: str, expected: str) -> None:
    response = client.post("/login", data={"password": PASSWORD, "next": next_url})
    assert response.headers["location"] == expected


def test_invalid_cookie_is_rejected(client: TestClient) -> None:
    client.cookies.set("jm_session", "forged-token")
    assert client.get("/").headers["location"] == "/login"


def test_expired_session_is_rejected(client: TestClient, password_settings: Settings) -> None:
    login(client)
    _shift_times(password_settings, "sessions", "expires_at", -timedelta(days=8))
    assert client.get("/").headers["location"] == "/login"
    assert _count(password_settings, "sessions") == 0


def test_session_absolute_lifetime(client: TestClient, password_settings: Settings) -> None:
    login(client)
    _shift_times(password_settings, "sessions", "created_at", -timedelta(days=31))
    assert client.get("/").headers["location"] == "/login"


def test_session_expiry_slides(client: TestClient, password_settings: Settings) -> None:
    login(client)
    _shift_times(password_settings, "sessions", "last_seen_at", -timedelta(hours=1))
    _shift_times(password_settings, "sessions", "expires_at", -timedelta(days=6))
    assert client.get("/").status_code == 200
    conn = _db(password_settings)
    (expires,) = conn.execute("SELECT expires_at FROM sessions").fetchone()
    conn.close()
    remaining = datetime.fromisoformat(expires) - utcnow().replace(tzinfo=None)
    assert remaining > timedelta(days=6, hours=23)


# --- CSRF ----------------------------------------------------------------------------------------


def test_csrf_token_required_for_authenticated_posts(client: TestClient) -> None:
    csrf = login(client)
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", data={"csrf_token": "x" * 64}).status_code == 403
    assert client.post("/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    # Still signed in after the rejected attempts.
    assert client.get("/").status_code == 200
    assert client.post("/logout", headers={"X-CSRF-Token": csrf}).status_code == 303


def test_csrf_token_differs_per_session(password_settings: Settings) -> None:
    with make_client(password_settings) as a, make_client(password_settings) as b:
        assert login(a) != login(b)


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
    ],
)
def test_cross_origin_posts_blocked_even_for_login(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.post("/login", data={"password": PASSWORD}, headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "headers", [{"Sec-Fetch-Site": "same-origin"}, {"Origin": "http://testserver"}, {}]
)
def test_same_origin_posts_allowed(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/login", data={"password": PASSWORD}, headers=headers)
    assert response.status_code == 303


def test_oversized_form_rejected(client: TestClient) -> None:
    login(client)
    response = client.post("/logout", data={"filler": "x" * (1024 * 1024 + 1)})
    assert response.status_code == 413


# --- rate limiting -------------------------------------------------------------------------------


def test_lockout_after_repeated_failures(client: TestClient, password_settings: Settings) -> None:
    for _ in range(MAX_FAILURES):
        assert client.post("/login", data={"password": "wrong password"}).status_code == 401
    locked = client.post("/login", data={"password": PASSWORD})
    assert locked.status_code == 429
    assert "Too many failed attempts" in locked.text
    assert "set-cookie" not in locked.headers

    _shift_times(password_settings, "login_attempts", "attempted_at", -timedelta(minutes=16))
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303


def test_success_resets_failure_count(client: TestClient) -> None:
    for _ in range(MAX_FAILURES - 1):
        client.post("/login", data={"password": "wrong password"})
    login(client)
    for _ in range(MAX_FAILURES - 1):
        assert client.post("/login", data={"password": "wrong password"}).status_code == 401
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303


def test_setup_token_guesses_are_rate_limited(setup_client: TestClient) -> None:
    data = {"token": "guess", "password": PASSWORD, "confirm": PASSWORD}
    for _ in range(MAX_FAILURES):
        assert setup_client.post("/setup", data=data).status_code == 403
    data["token"] = _setup_token(setup_client)
    assert setup_client.post("/setup", data=data).status_code == 429


# --- change password -----------------------------------------------------------------------------


def test_change_password_signs_out_other_sessions(password_settings: Settings) -> None:
    new_password = "a brand new passphrase"
    with make_client(password_settings) as me, make_client(password_settings) as other:
        csrf = login(me)
        login(other)

        wrong = me.post(
            "/settings/password",
            data={
                "csrf_token": csrf,
                "current": "nope",
                "password": new_password,
                "confirm": new_password,
            },
        )
        assert wrong.status_code == 403

        response = me.post(
            "/settings/password",
            data={
                "csrf_token": csrf,
                "current": PASSWORD,
                "password": new_password,
                "confirm": new_password,
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?changed=true"

        assert "Password changed" in me.get("/settings?changed=true").text  # still signed in
        assert other.get("/").headers["location"] == "/login"  # signed out
        assert other.post("/login", data={"password": PASSWORD}).status_code == 401
        login(other, new_password)


# --- cookie flags --------------------------------------------------------------------------------


def _cookie_attributes(set_cookie: str) -> set[str]:
    return {part.strip().split("=")[0].lower() for part in set_cookie.split(";")[1:]}


def test_cookie_flags_insecure_mode(client: TestClient) -> None:
    response = client.post("/login", data={"password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("jm_session=")
    assert {"httponly", "path", "samesite", "max-age"} <= _cookie_attributes(cookie)
    assert "secure" not in _cookie_attributes(cookie)
    assert "samesite=strict" in cookie.lower()
    assert "path=/" in cookie.lower()


def test_cookie_flags_secure_mode(password_settings: Settings) -> None:
    settings = password_settings.model_copy(update={"secure_cookies": True})
    with make_client(settings, base_url="https://testserver") as client:
        response = client.post("/login", data={"password": PASSWORD})
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-jm_session=")
        assert {"httponly", "secure", "path", "samesite"} <= _cookie_attributes(cookie)
        assert "domain" not in _cookie_attributes(cookie)  # required for the __Host- prefix
        assert client.get("/").status_code == 200
        # The insecure cookie name is not accepted in secure mode.
        token = client.cookies.get("__Host-jm_session")
        client.cookies.clear()
        client.cookies.set("jm_session", token or "")
        assert client.get("/").status_code == 303


def test_setup_error_keeps_token_but_not_passwords(setup_client: TestClient) -> None:
    token = _setup_token(setup_client)
    data = {"token": token, "password": PASSWORD, "confirm": PASSWORD + "x"}
    html = setup_client.post("/setup", data=data).text
    assert f'value="{token}"' in html
    assert PASSWORD not in html
