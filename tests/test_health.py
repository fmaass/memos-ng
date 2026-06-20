from tests.conftest import session_factory


def test_health_ok_when_db_reachable(client, monkeypatch):
    monkeypatch.setattr("app.main.async_session", session_factory(fail=False))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]  # version string is present
    assert body.get("db") is None


def test_health_degraded_when_db_unreachable(client, monkeypatch):
    # Fails if the try/except around the DB probe is removed (exception → 500, not 503),
    # or if status is hardcoded to "ok".
    monkeypatch.setattr("app.main.async_session", session_factory(fail=True))
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"


def test_unknown_api_route_returns_json_404(client):
    # Fails if the /api/* catch-all is removed (would fall through to SPA/503).
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("/api/")
