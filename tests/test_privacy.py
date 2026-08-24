"""GET /privacy — the published privacy policy, served to anyone.

Its whole point is reachability without a credential (App Store review fetches it, and so does
anyone deciding whether to sign in at all), so every test here drives an anonymous client, and the
last one is the positive control that the app has not simply stopped authenticating.
"""

from app.main import PRIVACY_PAGE, PUBLIC_OPERATIONS, app
from fastapi.testclient import TestClient

# A sentence from the writer-approved document. Quoted here rather than read out of the file, so
# the assertion cannot pass by comparing the response with itself.
KNOWN_PHRASE = "No analytics, no tracking, no advertising identifier, no ads."


def _anonymous() -> TestClient:
    """Empty cookie jar, no Authorization header — the conftest `client` carries neither either,
    but this states it rather than inheriting it."""
    return TestClient(app)


def test_privacy_is_served_to_an_anonymous_caller():
    r = _anonymous().get("/privacy")
    assert r.status_code == 200
    assert KNOWN_PHRASE in r.text


def test_privacy_is_served_as_html():
    # A policy delivered as application/json or as a download renders as source in a browser.
    assert _anonymous().get("/privacy").headers["content-type"].startswith("text/html")


def test_privacy_is_publicly_cacheable_unlike_media():
    """Every media response is `private` (app/routers/assets.py); one published document, identical
    for every reader and addressed to readers who have not signed in, is the deliberate exception.
    """
    cache = _anonymous().get("/privacy").headers["cache-control"]
    assert cache.startswith("public"), cache
    assert "max-age=" in cache, cache


def test_privacy_serves_the_committed_document_byte_for_byte():
    """The page is writer-approved: the route hands over that file, it does not render its own."""
    assert _anonymous().get("/privacy").content == PRIVACY_PAGE.read_bytes()


def test_privacy_beats_the_spa_catch_all():
    """`/{path:path}` answers every non-/api URL with the SPA shell (or, with no build present, a
    503) — it would swallow /privacy if the route were declared after it."""
    body = _anonymous().get("/privacy").text
    assert body.lstrip().startswith("<!doctype html>")
    assert 'id="app"' not in body
    assert "Frontend not built" not in body


def test_privacy_is_published_as_a_declared_public_operation():
    """The MEMNG-111 schema pass reads each route's requirement off its auth dependency; a route
    with none has to say so out loud, or tests/test_openapi_drift.py fails."""
    assert ("GET", "/privacy") in PUBLIC_OPERATIONS
    operation = app.openapi()["paths"]["/privacy"]["get"]
    assert "security" not in operation
    assert "text/html" in operation["responses"]["200"]["content"]


def test_a_protected_route_still_requires_a_credential():
    """Positive control: /privacy answering an anonymous caller is a property of that route, not of
    an app that has stopped checking credentials."""
    assert _anonymous().get("/api/memos").status_code == 401
