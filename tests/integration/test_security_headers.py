import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute, Mount, Route

from app.auth.middleware import PUBLIC_PATHS
from app.config import Settings
from app.security.headers import BASE_HEADERS, HSTS
from tests.integration.conftest import PASSWORD, login, make_client

# A sample of every kind of response: public page, redirect, auth rejection, API error,
# static file, 404, CSRF rejection.
PATHS = ["/healthz", "/login", "/", "/api/v1/session", "/static/css/app.css", "/nope"]


@pytest.mark.parametrize("path", PATHS)
def test_security_headers_on_every_response(client: TestClient, path: str) -> None:
    response = client.get(path)
    for name, value in BASE_HEADERS.items():
        assert response.headers.get(name) == value, name
    assert "strict-transport-security" not in response.headers


def test_security_headers_on_rejected_post(client: TestClient) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": "https://x.example"}
    )
    assert response.status_code == 403
    assert response.headers["content-security-policy"] == BASE_HEADERS["Content-Security-Policy"]


def test_csp_forbids_inline_and_framing(client: TestClient) -> None:
    csp = client.get("/login").headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "frame-ancestors 'none'" in csp


def test_pages_have_no_inline_scripts_or_styles(client: TestClient) -> None:
    pages = [client.get("/login").text]
    login(client)
    pages += [client.get("/").text, client.get("/settings").text]
    for html in pages:
        # External scripts only: every <script> needs a src, and no inline event handlers.
        assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html) == []
        assert re.findall(r"\son[a-z]+=", html) == []
        assert "<style" not in html
        assert " style=" not in html


def test_hsts_only_in_secure_mode(password_settings: Settings) -> None:
    settings = password_settings.model_copy(update={"secure_cookies": True})
    with make_client(settings, base_url="https://testserver") as client:
        assert client.get("/healthz").headers["strict-transport-security"] == HSTS


def test_cache_control(client: TestClient) -> None:
    assert client.get("/login").headers["cache-control"] == "no-store"
    login(client)
    assert client.get("/settings").headers["cache-control"] == "no-store"
    assert client.get("/static/css/app.css").headers["cache-control"] == "no-cache"


def _all_routes(routes: list[BaseRoute]) -> Iterator[BaseRoute]:
    """Flatten routes, including those inside FastAPI's included-router wrappers."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner.routes)
        else:
            yield route


def test_every_route_requires_login_unless_public(client: TestClient) -> None:
    """Default deny: any route added later is protected unless deliberately made public."""
    app = client.app
    checked = 0
    for route in _all_routes(app.routes):  # type: ignore[attr-defined]
        if isinstance(route, Mount):
            assert route.path == "/static"
            continue
        assert isinstance(route, Route), route
        if route.path in PUBLIC_PATHS:
            continue
        for method in route.methods or set():
            if method == "HEAD":
                continue
            response = client.request(method, route.path)
            assert response.status_code in (303, 401), (method, route.path)
            checked += 1
    assert checked >= 5


def test_every_page_offers_the_source_code(client: TestClient, setup_client: TestClient) -> None:
    """AGPL-3.0 section 13: network users must be offered the corresponding source."""
    from app import SOURCE_URL

    pages = [setup_client.get("/setup").text, client.get("/login").text]
    login(client)
    pages += [client.get(path).text for path in ("/", "/monitors", "/matches", "/settings")]
    for html in pages:
        assert f'<a href="{SOURCE_URL}"' in html
        assert "AGPL-3.0" in html
