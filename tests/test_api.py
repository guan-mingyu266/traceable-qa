from fastapi.testclient import TestClient

from qa.api import app

client = TestClient(app)


def test_health_reports_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unimplemented_routes_answer_501() -> None:
    response = client.post("/api/ask")
    assert response.status_code == 501

    response = client.post("/api/upload")
    assert response.status_code == 501

    response = client.get("/api/documents")
    assert response.status_code == 501
