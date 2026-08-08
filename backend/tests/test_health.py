"""Health endpoint tests."""

from fastapi.testclient import TestClient

from pet.main import app


def test_health_returns_installed_project_metadata() -> None:
    """The real endpoint reports an OK status and a non-empty version."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"]
