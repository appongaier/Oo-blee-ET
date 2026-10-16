import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import DISCLAIMER, app

client = TestClient(app)


def test_health_ok_and_disclaimer():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["disclaimer"] == DISCLAIMER


def test_graph_health_reports_unreachable_without_leaking(monkeypatch):
    # Point at a port nothing listens on: a real failed connection, no mocks.
    monkeypatch.setenv("FALKORDB_PORT", "1")
    monkeypatch.setenv("FALKORDB_PASSWORD", "super-secret")
    config.get_settings.cache_clear()
    try:
        r = client.get("/health/graph")
    finally:
        config.get_settings.cache_clear()
    assert r.status_code == 503
    body = r.json()
    assert body["reachable"] is False
    assert body["graphs"] == []
    assert "super-secret" not in r.text


def _falkordb_up() -> bool:
    config.get_settings.cache_clear()
    return db.check_falkordb()["reachable"]


@pytest.mark.integration
def test_graph_health_against_live_falkordb():
    if not _falkordb_up():
        pytest.skip("FalkorDB not running (docker compose up -d)")
    r = client.get("/health/graph")
    assert r.status_code == 200
    assert r.json()["reachable"] is True
