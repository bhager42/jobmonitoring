from fastapi.testclient import TestClient

from app import __version__
from tests.integration.conftest import login


def test_healthz_returns_ok_and_version(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_healthz_is_public_during_setup(setup_client: TestClient) -> None:
    assert setup_client.get("/healthz").status_code == 200


def test_api_docs_disabled(client: TestClient) -> None:
    login(client)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
