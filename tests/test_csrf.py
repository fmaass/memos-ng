"""Request-origin CSRF enforcement on cookie-authenticated mutations (app/csrf.py).

The threat this covers is estate-local: the session cookie is SameSite=Lax, which is SITE-scoped,
so a page on a SIBLING household subdomain (`*.discomarder.live`) is same-site and can make the
browser attach the memos-ng session cookie to a forged mutation. Only the browser-set
Origin/Referer distinguishes it from the SPA's own request.
"""

import pytest
from app.auth import SID_COOKIE
from app.config import settings
from app.csrf import canonical_origin, csrf_reject_reason, origin_of, stated_origin
from app.main import app
from fastapi.testclient import TestClient

# A sibling household app: same SITE, different ORIGIN — the live CSRF vector in this estate.
SIBLING = "https://other.discomarder.live"
COOKIE = {SID_COOKIE: "a-session-id"}


# --- pure logic -------------------------------------------------------------------------------


def test_canonical_origin_is_derived_from_the_oidc_redirect_uri(monkeypatch):
    # Production value: the redirect URI Authelia sends the browser back to.
    assert settings.OIDC_REDIRECT_URI == "https://memos-ng.discomarder.live/api/auth/callback"
    assert canonical_origin() == "https://memos-ng.discomarder.live"
    # It TRACKS that setting rather than hardcoding the host (a differently-hosted deployment,
    # or a dev instance behind the Vite proxy, is protected against its own origin).
    monkeypatch.setattr(settings, "OIDC_REDIRECT_URI", "http://localhost:5173/api/auth/callback")
    assert canonical_origin() == "http://localhost:5173"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://memos-ng.discomarder.live", "https://memos-ng.discomarder.live"),
        ("https://memos-ng.discomarder.live/api/auth/callback", "https://memos-ng.discomarder.live"),
        ("http://localhost:5173/x?y=1", "http://localhost:5173"),
        ("https://MEMOS-NG.Discomarder.Live/", "https://memos-ng.discomarder.live"),
        ("/api/memos", ""),  # not absolute → no origin at all
        ("", ""),
    ],
)
def test_origin_of_extracts_scheme_host_port(url, expected):
    assert origin_of(url) == expected


def test_stated_origin_prefers_origin_and_falls_back_to_referer():
    assert stated_origin("https://a.example", "https://b.example/p") == "https://a.example"
    assert stated_origin(None, "https://b.example/p/q?r=1") == "https://b.example"
    assert stated_origin(None, None) == ""
    # An opaque origin (sandboxed iframe) is never this app's, and must not fall through to a
    # Referer the same context could still be sending.
    assert stated_origin("null", "https://memos-ng.discomarder.live/x") == ""


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_safe_methods_are_never_refused(method):
    assert csrf_reject_reason(method, {"origin": SIBLING}, COOKIE) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_refuses_a_sibling_origin(method):
    reason = csrf_reject_reason(method, {"origin": SIBLING}, COOKIE)
    assert reason is not None and SIBLING in reason


def test_same_origin_mutation_is_allowed():
    assert csrf_reject_reason("POST", {"origin": canonical_origin()}, COOKIE) is None


def test_referer_fallback_is_compared_the_same_way():
    assert csrf_reject_reason("POST", {"referer": f"{canonical_origin()}/memos/x"}, COOKIE) is None
    assert csrf_reject_reason("POST", {"referer": f"{SIBLING}/attack.html"}, COOKIE) is not None


def test_both_headers_absent_is_refused():
    assert csrf_reject_reason("POST", {}, COOKIE) == (
        "Missing Origin/Referer on a cookie-authenticated request"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://memos-ng.discomarder.live.evil.example",  # suffix graft
        "https://evil.example/?u=https://memos-ng.discomarder.live",  # substring in the query
        "http://memos-ng.discomarder.live",  # scheme downgrade
        "https://memos-ng.discomarder.live:8443",  # different port
        "https://discomarder.live",  # the parent site is not this origin
        "null",  # opaque origin
    ],
)
def test_look_alike_origins_are_refused(origin):
    assert csrf_reject_reason("POST", {"origin": origin}, COOKIE) is not None


def test_requests_without_a_session_cookie_are_exempt():
    # Satellites/curl authenticate with a PAT (or not at all) and carry no ambient authority.
    assert csrf_reject_reason("POST", {"origin": SIBLING}, {}) is None
    assert csrf_reject_reason("POST", {}, {}) is None
    assert csrf_reject_reason("POST", {"authorization": "Bearer memos_pat_abc"}, {}) is None


def test_a_bearer_header_does_not_exempt_a_cookie_carrying_request():
    """Stricter than a blanket Bearer exemption, deliberately: every mutation route today depends
    on `require_user`, which ignores Authorization and authenticates from the cookie — so trusting
    the header would hand any header-setting caller a way around this check."""
    assert (
        csrf_reject_reason(
            "POST", {"origin": SIBLING, "authorization": "Bearer memos_pat_abc"}, COOKIE
        )
        is not None
    )


# --- through the real app (middleware wired in app.main) ----------------------------------------


def test_sibling_origin_is_refused_on_a_cookie_authenticated_mutation(logged_in_client):
    """AC 1 + AC 2: a mutation forged by a sibling *.discomarder.live page is refused, and the
    route's side effect never runs."""
    client, _ = logged_in_client
    before = client.get("/api/memos").json()["items"]

    r = client.post(
        "/api/memos",
        json={"content": "forged by a sibling", "visibility": "PUBLIC"},
        headers={"Origin": SIBLING},
    )

    assert r.status_code == 403
    assert SIBLING in r.json()["detail"]
    after = client.get("/api/memos").json()["items"]
    assert len(after) == len(before)
    assert all(m["content"] != "forged by a sibling" for m in after)


def test_same_origin_mutation_still_succeeds(logged_in_client):
    client, _ = logged_in_client
    r = client.post("/api/memos", json={"content": "from the SPA", "visibility": "PUBLIC"})
    assert r.status_code == 201


def test_cookie_mutation_without_origin_or_referer_is_refused(logged_in_client):
    client, _ = logged_in_client
    del client.headers["Origin"]  # a request that states nothing about where it came from
    r = client.post("/api/memos", json={"content": "headerless", "visibility": "PUBLIC"})
    assert r.status_code == 403
    assert client.get("/api/memos?q=headerless").json()["items"] == []


def test_referer_only_mutation_is_accepted_when_it_matches(logged_in_client):
    client, _ = logged_in_client
    del client.headers["Origin"]
    r = client.post(
        "/api/memos",
        json={"content": "referer only", "visibility": "PUBLIC"},
        headers={"Referer": f"{canonical_origin()}/memos"},
    )
    assert r.status_code == 201


def test_delete_from_a_sibling_origin_leaves_the_memo_intact(logged_in_client):
    client, _ = logged_in_client
    uid = client.post("/api/memos", json={"content": "keep me", "visibility": "PUBLIC"}).json()[
        "uid"
    ]
    assert client.delete(f"/api/memos/{uid}", headers={"Origin": SIBLING}).status_code == 403
    assert client.get(f"/api/memos/{uid}").status_code == 200  # side effect never ran


def test_logout_is_protected_and_the_own_origin_flow_still_works(logged_in_client):
    client, _ = logged_in_client
    assert client.post("/api/auth/logout", headers={"Origin": SIBLING}).status_code == 403
    assert client.get("/api/auth/user").status_code == 200  # session NOT destroyed by the forgery

    assert client.post("/api/auth/logout").status_code == 200  # the SPA's own logout is untouched
    assert client.get("/api/auth/user").status_code == 401


def test_the_rejection_still_carries_the_security_headers(logged_in_client):
    """SecurityHeadersMiddleware must stay OUTSIDE the CSRF middleware (it is added last in
    app.main, so it wraps it) — otherwise rejections would ship without CSP/nosniff."""
    client, _ = logged_in_client
    r = client.post("/api/memos", json={"content": "x"}, headers={"Origin": SIBLING})
    assert r.status_code == 403
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_safe_methods_are_untouched_by_a_foreign_origin(logged_in_client):
    client, _ = logged_in_client
    assert client.get("/api/memos", headers={"Origin": SIBLING}).status_code == 200
    assert client.get("/api/auth/user", headers={"Origin": SIBLING}).status_code == 200


def test_a_cookieless_client_keeps_working_headerlessly():
    """PAT satellites and curl send no session cookie, so the check never fires: the request
    reaches the route and is judged by authentication alone (401 here — never a CSRF 403)."""
    anon = TestClient(app, raise_server_exceptions=False)  # no session cookie, no Origin default
    assert anon.post("/api/memos", json={"content": "x"}).status_code == 401
    r = anon.post(
        "/api/memos",
        json={"content": "x"},
        headers={"Origin": SIBLING, "Authorization": "Bearer memos_pat_whatever"},
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/uploads", {"filename": "a.jpg", "mime": "image/jpeg", "size": 10}),
        ("PATCH", "/api/uploads/nonexistent", None),
        ("POST", "/api/pats", {"name": "forged"}),
        ("DELETE", "/api/pats/1", None),
        ("PATCH", "/api/settings", {"theme": "light"}),
        ("POST", "/api/push/subscribe", {"endpoint": "https://x", "keys": {}}),
        ("DELETE", "/api/push/subscribe", None),
        ("DELETE", "/api/attachments/nonexistent", None),
        ("POST", "/api/attachments/nonexistent/print", None),
        ("POST", "/api/memos/nonexistent/pin", None),
        ("POST", "/api/memos/nonexistent/reactions", {"emoji": "x"}),
        ("POST", "/api/unmatched/route", None),  # even the /api/* 404 fallback is behind the check
    ],
)
def test_enforcement_is_central_not_per_route(logged_in_client, method, path, body):
    """Every unsafe surface is covered by the one middleware — including paths whose handler would
    answer 404/422, which proves the check runs BEFORE routing and handler side effects."""
    client, _ = logged_in_client
    r = client.request(method, path, json=body, headers={"Origin": SIBLING})
    assert r.status_code == 403, f"{method} {path} was not refused"
