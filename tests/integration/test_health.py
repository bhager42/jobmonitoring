from fastapi.testclient import TestClient

from app import __version__
from app.main import create_app


def test_healthz_returns_ok_and_version() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_api_docs_disabled() -> None:
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
