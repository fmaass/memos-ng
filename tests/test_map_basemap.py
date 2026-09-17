"""The self-hosted basemap surface: archive range serving, style assets, manifest, CSP.

The archives are multi-gigabyte files a map client reads by byte range, so the contract that
matters here is not "the bytes came back" but *which* bytes, under which status, holding which
resources. Three properties are load-bearing and each has a failure that looks like success:

* **Range semantics.** A 200 carrying the whole file in answer to a `Range` request "works" in a
  test that only checks for content — and makes the client download gigabytes per tile. So the
  status, the `Content-Range` and the exact bytes are all asserted.
* **Containment.** The asset route takes a free-form path. A traversal that resolves outside the
  served directory must 404, not serve; both the raw and the percent-encoded spellings are probed.
* **Connection lifetime.** Authentication needs the database; the transfer does not. A route that
  authenticates through a yield-based dependency holds a pool connection for the whole download,
  which is how a handful of concurrent archive fetches exhausts the shared instance. The order
  test below observes the real sequence rather than the code's shape.
"""

import os
import re
from pathlib import Path

import pytest
from app.config import settings
from app.database import engine as async_engine
from app.main import app
from app.security_headers import CSP
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import event

# The whole CSP as it stood before the basemap work, directive by directive. Frozen here on
# purpose: the change under test is additive, and the only way to prove a directive did not go
# missing (or quietly widen) is to compare against a recorded baseline rather than against the
# string the app happens to serve today.
CSP_BEFORE = (
    "default-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
)

PLANET = "planet-20260917.pmtiles"
DETAIL = "detail-20260917-a1b2c3d4.pmtiles"
# Not the real PMTiles spec header — the routes serve opaque bytes and must not care. What the
# body assertions need is a payload long enough to slice ranges out of and impossible to produce
# by accident.
ARCHIVE_BYTES = b"PMTiles-fake-archive-" + bytes(range(256)) * 8


@pytest.fixture
def tiles_dir(tmp_path, monkeypatch) -> Path:
    """A populated tiles directory, with TILES_DIR pointed at it."""
    root = tmp_path / "tiles"
    (root / "assets" / "fonts" / "Noto Sans Regular").mkdir(parents=True)
    (root / "assets" / "sprites" / "v4").mkdir(parents=True)
    (root / PLANET).write_bytes(ARCHIVE_BYTES)
    (root / "BUILD").write_text("build=20260917\n")
    (root / "assets" / "fonts" / "Noto Sans Regular" / "0-255.pbf").write_bytes(b"glyph-bytes")
    (root / "assets" / "sprites" / "v4" / "light.json").write_text('{"circle":{}}')
    (root / "assets" / ".hidden").write_text("secret")
    monkeypatch.setattr(settings, "TILES_DIR", str(root))
    return root


def _tile(name: str = PLANET) -> str:
    return f"/api/map/tiles/{name}"


# --- authentication -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [_tile(), "/api/map/assets/sprites/v4/light.json", "/api/map/manifest"],
)
def test_the_basemap_surface_is_closed_to_anonymous_callers(client, tiles_dir, path):
    """Every byte of the basemap is behind the household login — a map request states where
    someone has been just as plainly as the pins do."""
    assert client.get(path).status_code == 401


def test_a_bearer_token_reaches_the_archive(logged_in_client, tiles_dir):
    """The native client authenticates with a token and no cookie. A jar-less client is the point:
    reusing the session client with an extra header would still pass with the bearer path gone."""
    session_client, _ = logged_in_client
    created = session_client.post("/api/pats", json={"name": "map-device"})
    assert created.status_code == 201, created.text
    bearer = TestClient(app, raise_server_exceptions=False)
    bearer.headers["Authorization"] = f"Bearer {created.json()['token']}"
    assert bearer.get(_tile()).status_code == 200


# --- range semantics ------------------------------------------------------------------------


def test_a_byte_range_returns_exactly_those_bytes(logged_in_client, tiles_dir):
    client, _ = logged_in_client
    r = client.get(_tile(), headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == ARCHIVE_BYTES[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(ARCHIVE_BYTES)}"
    assert r.headers["content-length"] == "10"


def test_a_range_starting_past_the_end_is_not_satisfiable(logged_in_client, tiles_dir):
    client, _ = logged_in_client
    r = client.get(_tile(), headers={"Range": f"bytes={len(ARCHIVE_BYTES) + 1}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(ARCHIVE_BYTES)}"


def test_without_a_range_the_whole_archive_is_served(logged_in_client, tiles_dir):
    client, _ = logged_in_client
    r = client.get(_tile())
    assert r.status_code == 200
    assert r.content == ARCHIVE_BYTES
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["cache-control"] == "private, max-age=604800, immutable"
    assert r.headers["etag"]


def test_head_states_the_size_without_sending_the_archive(logged_in_client, tiles_dir):
    client, _ = logged_in_client
    r = client.head(_tile())
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(ARCHIVE_BYTES))
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b""


def test_if_range_honours_the_range_only_while_the_archive_is_the_one_cached(
    logged_in_client, tiles_dir
):
    """A client resuming a partial download sends `If-Range` with the validator it holds. Matching
    → the range it asked for; stale → the whole file, because a resumed transfer must never splice
    new bytes onto old ones. There is no 304 on this path: a conditional GET of a changed archive
    answers 200 with the body, and that is the contract asserted, not one assumed."""
    client, _ = logged_in_client
    etag = client.head(_tile()).headers["etag"]

    fresh = client.get(_tile(), headers={"Range": "bytes=0-9", "If-Range": etag})
    assert fresh.status_code == 206
    assert fresh.content == ARCHIVE_BYTES[:10]

    stale = client.get(_tile(), headers={"Range": "bytes=0-9", "If-Range": '"not-the-one"'})
    assert stale.status_code == 200
    assert stale.content == ARCHIVE_BYTES


# --- the name whitelist ---------------------------------------------------------------------


def test_a_named_archive_that_is_not_there_is_a_plain_not_found(logged_in_client, tiles_dir):
    """GET and HEAD alike: the existence check runs before the file response is constructed, so a
    missing archive is a 404 and never a 500 raised out of the streaming layer."""
    client, _ = logged_in_client
    absent = _tile("planet-19990101.pmtiles")
    assert client.get(absent).status_code == 404
    assert client.head(absent).status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "../BUILD",
        "%2e%2e/BUILD",
        "..%2fBUILD",
        "/etc/passwd",
        "BUILD",
        "planet-20260917.pmtiles.bak",
        "planet-20260917.PMTILES",
        "planet-../-20260917.pmtiles",
        "anything-20260917.pmtiles",
        ".planet-20260917.pmtiles",
    ],
)
def test_only_whitelisted_archive_names_are_reachable(logged_in_client, tiles_dir, name):
    client, _ = logged_in_client
    r = client.get(f"/api/map/tiles/{name}")
    assert r.status_code == 404, r.text
    assert ARCHIVE_BYTES[:8] not in r.content


def test_the_whitelist_pattern_accepts_the_names_the_extract_script_publishes():
    """A positive control for the parametrized refusals above: without it a pattern that matched
    nothing at all would pass every one of them."""
    from app.routers.map import ARCHIVE_NAME

    assert ARCHIVE_NAME.fullmatch(PLANET)
    assert ARCHIVE_NAME.fullmatch(DETAIL)


# --- style assets ---------------------------------------------------------------------------


def test_style_assets_are_served_from_under_the_tiles_directory(logged_in_client, tiles_dir):
    client, _ = logged_in_client
    glyphs = client.get("/api/map/assets/fonts/Noto Sans Regular/0-255.pbf")
    assert glyphs.status_code == 200
    assert glyphs.content == b"glyph-bytes"
    assert glyphs.headers["content-type"].startswith("application/")
    sprite = client.get("/api/map/assets/sprites/v4/light.json")
    assert sprite.status_code == 200
    assert sprite.json() == {"circle": {}}


@pytest.mark.parametrize(
    "path",
    [
        "../BUILD",
        "../../etc/passwd",
        "%2e%2e/BUILD",
        "fonts/../../BUILD",
        "/etc/passwd",
        ".hidden",
        "fonts/../.hidden",
        "sprites/v4/missing.json",
    ],
)
def test_no_asset_path_escapes_the_served_directory(logged_in_client, tiles_dir, path):
    client, _ = logged_in_client
    r = client.get(f"/api/map/assets/{path}")
    assert r.status_code == 404, r.text
    assert b"secret" not in r.content
    assert b"build=" not in r.content


# --- the manifest ---------------------------------------------------------------------------


def test_the_manifest_names_the_archives_that_are_there(logged_in_client, tiles_dir):
    client, _ = logged_in_client
    r = client.get("/api/map/manifest")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["build"] == "20260917"
    assert body["planet"]["name"] == PLANET
    assert body["planet"]["url"] == _tile()
    assert body["planet"]["size"] == len(ARCHIVE_BYTES)
    # Nothing extracted for the pin regions yet — the map still has to render, off the planet
    # archive alone, so the absence is reported rather than faked.
    assert body["detail"] is None


def test_a_new_generation_takes_over_the_manifest_while_the_previous_file_stays(
    logged_in_client, tiles_dir
):
    """Re-extracting publishes a new file name and leaves the old one on disk; the manifest must
    follow the new one. A client holding the previous archive's directory keeps reading a file
    that is still there, which is the whole reason the name carries the version."""
    client, _ = logged_in_client
    (tiles_dir / DETAIL).write_bytes(ARCHIVE_BYTES)
    first = client.get("/api/map/manifest").json()
    assert first["detail"]["name"] == DETAIL
    assert client.get("/api/map/manifest").json() == first  # stable while nothing is published

    newer = "detail-20260917-99887766.pmtiles"
    (tiles_dir / newer).write_bytes(ARCHIVE_BYTES)
    # Publication order is what decides, and on disk that is the modification time. Stated
    # explicitly so the case does not rest on two writes landing in different filesystem ticks.
    os.utime(tiles_dir / newer, (2_000_000_000, 2_000_000_000))
    second = client.get("/api/map/manifest").json()
    assert second["detail"]["name"] == newer
    assert (tiles_dir / DETAIL).exists()
    assert client.get(_tile(DETAIL)).status_code == 200


def test_an_empty_tiles_directory_reports_nothing_rather_than_failing(
    logged_in_client, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "TILES_DIR", str(tmp_path / "never-extracted"))
    body = client.get("/api/map/manifest").json()
    assert body == {"build": None, "planet": None, "detail": None}


def test_the_tiles_directory_defaults_to_the_derived_asset_volume(monkeypatch):
    from app.services.asset_paths import tiles_dir as resolve

    monkeypatch.setattr(settings, "DATA_ROOT", "/srv/memos")
    monkeypatch.setattr(settings, "TILES_DIR", "")
    assert resolve() == Path("/srv/memos/assets/tiles")
    monkeypatch.setattr(settings, "TILES_DIR", "/mnt/nvme/tiles")
    assert resolve() == Path("/mnt/nvme/tiles")


# --- connection lifetime --------------------------------------------------------------------


def test_the_archive_transfer_holds_no_database_connection(
    logged_in_client, tiles_dir, monkeypatch
):
    """The database connection is back in the pool before the first byte leaves.

    Observed, not inspected: the pool's own check-in event and the start of the file response are
    both recorded, and the order of the two is the assertion. Authenticating through a yield-based
    dependency would hold the connection open across the transfer and land the events the other
    way round.
    """
    client, _ = logged_in_client
    events: list[str] = []

    class RecordingFileResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            events.append("stream")
            return await super().__call__(scope, receive, send)

    import app.routers.map as map_router

    monkeypatch.setattr(map_router, "FileResponse", RecordingFileResponse)

    def on_checkin(*_args):
        events.append("checkin")

    event.listen(async_engine.sync_engine, "checkin", on_checkin)
    try:
        assert client.get(_tile()).status_code == 200
    finally:
        event.remove(async_engine.sync_engine, "checkin", on_checkin)

    assert "checkin" in events, events  # positive control: the route did use the database
    assert events.index("checkin") < events.index("stream"), events


# --- content security policy ----------------------------------------------------------------


def test_the_policy_gains_the_worker_directive_and_nothing_else(client):
    """A map renders its vector tiles in web workers it starts from blob URLs; under a policy with
    no `worker-src`, `default-src 'self'` refuses them and the map is blank. `child-src` is the
    directive NOT used: this policy names no `frame-src`, so `child-src` would silently become the
    frame policy too — a widening nobody asked for."""
    served = client.get("/health").headers["content-security-policy"]
    assert served == CSP
    directives = served.split("; ")
    assert "worker-src 'self' blob:" in directives
    assert [d for d in directives if d != "worker-src 'self' blob:"] == list(CSP_BEFORE)
    assert not re.search(r"\b(child|frame)-src\b", served)
