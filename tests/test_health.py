import pytest
from app.version import API_MAJOR

from tests.conftest import session_factory


def test_health_ok_when_db_reachable(client, monkeypatch):
    monkeypatch.setattr("app.main.async_session", session_factory(fail=False))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]  # version string is present
    assert body["db"] == "ok"  # DB probe succeeded


def test_health_degraded_when_db_unreachable(client, monkeypatch):
    # Fails if the try/except around the DB probe is removed (exception → 500, not 503),
    # or if status is hardcoded to "ok".
    monkeypatch.setattr("app.main.async_session", session_factory(fail=True))
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"


@pytest.mark.parametrize(("fail", "status"), [(False, 200), (True, 503)])
def test_health_reports_api_major(client, monkeypatch, fail, status):
    # A client decides whether it can talk to this server from /health alone, and a degraded
    # server is exactly when it has to: both branches carry the major, as an integer.
    monkeypatch.setattr("app.main.async_session", session_factory(fail=fail))
    r = client.get("/health")
    assert r.status_code == status
    assert r.json()["apiMajor"] == API_MAJOR
    assert type(r.json()["apiMajor"]) is int


def test_unknown_api_route_returns_json_404(client):
    # Fails if the /api/* catch-all is removed (would fall through to SPA/503).
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("/api/")
