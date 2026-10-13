from fastapi.testclient import TestClient

from app.main import create_app


def test_health_ok():
    c = TestClient(create_app())
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.1.0"}
