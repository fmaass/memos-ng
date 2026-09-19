from pathlib import Path

import pytest
from app.main import FRONTEND_DIR, app, mount_spa, resolve_spa_file
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Match, Route

INDEX = FRONTEND_DIR / "index.html"

SPA_CATCH_ALL = "/{path:path}"
MEMO_PERMALINK = "/m/01J8Z3K9QW7X2E5R6T7Y8U9I0P"

IMMUTABLE = "public, max-age=31536000, immutable"

# The two sides of the compressor's minimum size (500 bytes in Starlette): the first stands in for
# the real map chunk (~1 MB, and past the 64 KB mark where FileResponse starts streaming), the
# second for the smallest thing a build can emit.
MAP_CHUNK = b"export const tile = () => 'xyzzy'\n" * 6000
TINY_CHUNK = b"export default 1\n"

SHELL_HTML = "<!doctype html><title>memos-ng</title><script src=/assets/index-abcd1234.js></script>"
SERVICE_WORKER = "self.addEventListener('push', () => {})\n"
MANIFEST = '{"name": "memos-ng"}'


@pytest.fixture
def spa_dist(tmp_path: Path) -> Path:
    """A dist tree shaped like the Vite build's: hashed files under assets/, the shell above it."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "map-abcd1234.js").write_bytes(MAP_CHUNK)
    (dist / "assets" / "tiny-abcd1234.js").write_bytes(TINY_CHUNK)
    (dist / "index.html").write_text(SHELL_HTML)
    (dist / "sw.js").write_text(SERVICE_WORKER)
    (dist / "manifest.webmanifest").write_text(MANIFEST)
    return dist


@pytest.fixture
def spa_client(spa_dist: Path) -> TestClient:
    """The real SPA tiers over that tree.

    frontend/dist is a build output and never exists in a checkout, so the app under test here
    runs the branch of `mount_spa` that only the image otherwise reaches — the headers and the
    compressor are the ones production gets, not a re-implementation.
    """
    built = FastAPI()
    mount_spa(built, spa_dist)
    return TestClient(built)


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


def test_hashed_asset_may_be_kept_forever(spa_client):
    # Every name under assets/ carries a content hash, so the URL cannot change meaning: the
    # phone may answer /places from disk instead of spending a VPN round-trip revalidating.
    response = spa_client.get("/assets/map-abcd1234.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE


def test_hashed_asset_is_compressed_for_a_client_that_accepts_it(spa_client):
    response = spa_client.get("/assets/map-abcd1234.js", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    # httpx decoded the wire body, so this equality IS the gzip round-trip: a body that did not
    # decompress to the file byte-for-byte fails here (or blows up in the decoder).
    assert response.content == MAP_CHUNK
    # Without this a shared cache may hand the compressed body to a client that cannot read it.
    assert "accept-encoding" in response.headers["vary"].lower()


def test_hashed_asset_stays_identity_for_a_client_that_does_not(spa_client):
    response = spa_client.get("/assets/map-abcd1234.js", headers={"Accept-Encoding": "identity"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.content == MAP_CHUNK


def test_asset_below_the_compression_floor_is_sent_as_it_is(spa_client):
    # Pins the other side of the threshold: a tiny file is served whole, still immutable.
    response = spa_client.get("/assets/tiny-abcd1234.js", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.content == TINY_CHUNK
    assert response.headers["cache-control"] == IMMUTABLE


def test_hashed_asset_still_answers_a_conditional_request(spa_client):
    # The compressor sits between StaticFiles and the client; a 304 must survive it, or every
    # revalidation would ship the whole megabyte again.
    first = spa_client.get("/assets/map-abcd1234.js", headers={"Accept-Encoding": "gzip"})

    second = spa_client.get(
        "/assets/map-abcd1234.js",
        headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["etag"]},
    )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["cache-control"] == IMMUTABLE


@pytest.mark.parametrize(
    ("path", "expected_body"),
    [
        ("/", SHELL_HTML),
        ("/places/nested/route", SHELL_HTML),
        ("/sw.js", SERVICE_WORKER),
        ("/manifest.webmanifest", MANIFEST),
    ],
)
def test_every_unhashed_shell_file_is_revalidated(spa_client, path, expected_body):
    # These names are stable, so a client that caches them heuristically can keep an index.html
    # naming chunks a deploy has already removed. `no-cache` means revalidate, not "do not store":
    # the ETag FileResponse sends makes the steady state a 304.
    response = spa_client.get(path)

    assert response.status_code == 200
    assert response.text == expected_body
    assert response.headers["cache-control"] == "no-cache"


def test_missing_asset_is_a_real_404_not_the_shell(spa_client):
    # The mount answers /assets/* itself; if it ever fell through to the catch-all, a stale
    # index.html asking for a deleted chunk would get HTML with a 200 and fail as a syntax error.
    response = spa_client.get("/assets/nope-00000000.js")

    assert response.status_code == 404
    assert SHELL_HTML not in response.text


def test_compression_never_leaves_the_assets_mount(client):
    # The scope control. The compressor is the mounted app's own wrapper, never app-wide: reach
    # for app.add_middleware(GZipMiddleware) and this goes red — which is the point, because
    # app-wide it would also hit the media tiers, the Range-served basemap archives and the
    # /api/events SSE stream.
    assert all(middleware.cls is not GZipMiddleware for middleware in app.user_middleware)

    for path in ("/health", "/openapi.json"):
        response = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in response.headers, f"{path} came back compressed"

    # ...and the control has teeth only if one of those bodies is far above any compressor's
    # minimum size, which /openapi.json (the whole schema) is.
    assert len(client.get("/openapi.json").content) > 2000
