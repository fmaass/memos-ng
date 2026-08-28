from app.main import FRONTEND_DIR, app, resolve_spa_file
from starlette.routing import Match, Route

INDEX = FRONTEND_DIR / "index.html"

SPA_CATCH_ALL = "/{path:path}"
MEMO_PERMALINK = "/m/01J8Z3K9QW7X2E5R6T7Y8U9I0P"


def matched_route(path: str, method: str = "GET") -> Route | None:
    """The route the app would dispatch `path` to — the first FULL match, as Starlette picks it.

    Resolving the real route table is the only way to test the SPA catch-all's *position*.
    `resolve_spa_file` below answers "which file for a path the catch-all already got"; it
    cannot see a registration-order regression that hands /api/* or /file/* to the shell
    before their own routers ever match.
    """
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": []}
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_parent_traversal_returns_index():
    # `../` escaping the dist dir must never resolve to a real outside file.
    assert resolve_spa_file("../../../../../../etc/passwd") == INDEX


def test_prefix_sibling_dir_is_not_served():
    # Regression for the lexical-startswith bug: a sibling dir sharing the "dist"
    # prefix (frontend/dist-secret) passes a `startswith(dist)` check but is NOT
    # under dist. With a real file present, the old code would have leaked it.
    sibling = FRONTEND_DIR.parent / "dist-secret"
    sibling.mkdir(parents=True, exist_ok=True)
    secret = sibling / "secret.txt"
    secret.write_text("SECRET")
    try:
        result = resolve_spa_file("../dist-secret/secret.txt")
        assert result != secret  # must not serve the sibling file
        assert result == INDEX
    finally:
        secret.unlink(missing_ok=True)
        sibling.rmdir()


def test_unknown_spa_path_returns_index():
    assert resolve_spa_file("explore/some/deep/route") == INDEX


def test_memo_permalink_returns_index():
    # /m/<uid> is the canonical memo URL (and the AASA applinks pattern the app claims). It is a
    # SPA route with no file behind it, so the shell has to answer it — otherwise a cold open or a
    # reload of a shared link 404s while in-app navigation to the same URL works.
    assert resolve_spa_file("m/01J8Z3K9QW7X2E5R6T7Y8U9I0P") == INDEX


def test_memo_permalink_is_dispatched_to_the_spa_catch_all():
    # The other half of "the shell answers a shared link": no earlier route may claim /m/*.
    # A uid is opaque and arbitrary, so the guarantee has to hold for any shape of it.
    for path in (MEMO_PERMALINK, "/m/anything", "/m/with/slashes", "/m/"):
        route = matched_route(path)
        assert route is not None, f"{path} matches no route at all"
        assert route.path == SPA_CATCH_ALL, f"{path} is claimed by {route.path}, not the SPA shell"


def test_spa_catch_all_does_not_shadow_the_api_or_file_tiers():
    # The catch-all is registered last precisely so these keep their own handlers. Reorder the
    # registrations in app/main.py and this is what breaks — every one of these would start
    # answering with SPA HTML: the REST API, the 0.29.1 compat tier that unmigrated satellites
    # still read, the association file Apple refuses as HTML, and the anonymous /health probe.
    for path in (
        "/api/memos",
        "/api/unmatched/route",
        "/api/v1/memos",
        "/file/attachments/abc/IMG.jpeg",
        "/.well-known/apple-app-site-association",
        "/health",
        "/privacy",
    ):
        route = matched_route(path)
        assert route is not None, f"{path} matches no route at all"
        assert route.path != SPA_CATCH_ALL, f"{path} is shadowed by the SPA catch-all"


def test_api_404_is_json_not_the_spa_shell(client):
    # Live request, not route introspection: an unmatched /api/* path must answer JSON, or every
    # API client's error handling starts parsing an HTML page.
    response = client.get("/api/definitely/not/a/route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "not found" in response.json()["detail"]


def test_memo_permalink_is_served_over_http(client):
    # The end-to-end shape of a shared link opening cold. Which branch of the SPA mount is live
    # depends on whether frontend/dist exists — it is gitignored, so a fresh checkout (and this
    # worktree) runs the "not built" branch while a built tree and the Docker image serve the
    # shell. Both outcomes come from the catch-all, and both fail if anything shadows /m/*.
    response = client.get(MEMO_PERMALINK)

    if FRONTEND_DIR.is_dir():
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
    else:
        assert response.status_code == 503
        assert response.json()["error"].startswith("Frontend not built")
