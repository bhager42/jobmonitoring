import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app

PASSWORD = "correct horse battery staple"


def make_client(settings: Settings, **kwargs: Any) -> TestClient:
    app = create_app(settings, failed_login_delay=0, start_scheduler=False)
    return TestClient(app, follow_redirects=False, **kwargs)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, "no CSRF meta tag in page"
    return match.group(1)


def login(client: TestClient, password: str = PASSWORD) -> str:
    """Sign in and return the CSRF token for the new session."""
    response = client.post("/login", data={"password": password})
    assert response.status_code == 303, response.text
    page = client.get("/")
    assert page.status_code == 200
    return csrf_from(page.text)


@pytest.fixture
def password_settings(migrated_settings: Settings) -> Settings:
    return migrated_settings.model_copy(update={"app_password": SecretStr(PASSWORD)})


@pytest.fixture
def client(password_settings: Settings) -> Iterator[TestClient]:
    with make_client(password_settings) as client:
        yield client


@pytest.fixture
def setup_client(migrated_settings: Settings) -> Iterator[TestClient]:
    """A fresh install with no password: setup mode."""
    with make_client(migrated_settings) as client:
        yield client
