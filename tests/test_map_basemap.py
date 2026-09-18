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

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from app.config import settings
from app.database import engine as async_engine
from app.main import app
from app.security_headers import CSP
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import event

from tests.conftest import SYNC_URL

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


# --- the assets revision the style versions its URLs with ------------------------------------

ASSETS_COMMIT = "028c18f713baecad011301ff7a69acc39bcc2ae7"


def test_the_manifest_states_which_assets_revision_is_being_served(logged_in_client, tiles_dir):
    """The glyphs and sprites are overwritten IN PLACE at fixed paths when their pin is bumped, so
    nothing about their URL changes on its own. The manifest carrying the recorded commit is what
    lets the style put it in the URL, which is the only thing that can end a week-long immutable
    cache of the previous revision."""
    client, _ = logged_in_client
    (tiles_dir / "BUILD").write_text(
        f"build=20260917\nassets_repo=protomaps/basemaps-assets\nassets_commit={ASSETS_COMMIT}\n"
    )
    body = client.get("/api/map/manifest").json()
    assert body["assets_commit"] == ASSETS_COMMIT
    assert body["build"] == "20260917"  # the other keys of the same file still read


def test_an_unrecorded_assets_revision_is_reported_absent_rather_than_guessed(
    logged_in_client, tiles_dir
):
    """A tiles directory whose assets were never fetched has no pin to state. Null rather than a
    placeholder: the style omits the query entirely instead of versioning URLs against a value
    that describes nothing."""
    client, _ = logged_in_client
    assert "assets_" not in (tiles_dir / "BUILD").read_text()
    assert client.get("/api/map/manifest").json()["assets_commit"] is None


def test_a_style_asset_is_cached_for_a_week_only_because_its_url_carries_the_revision(
    logged_in_client, tiles_dir
):
    """`immutable` on a path that IS rewritten in place (fetch-assets.sh overwrites fonts/ and
    sprites/) is only truthful while the client asks under a URL that changes with the pin. The
    header and the versioned query are one decision, so the header is asserted here and the query
    in the style's own spec."""
    client, _ = logged_in_client
    r = client.get(f"/api/map/assets/sprites/v4/light.json?v={ASSETS_COMMIT[:8]}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, max-age=604800, immutable"


@pytest.fixture(autouse=True)
def _empty_coverage_cache():
    """The uncovered count is cached per viewer and archive for ten minutes, and every case here
    speaks as the same viewer about the same archive names."""
    from app.services import map_coverage

    map_coverage._CACHE.clear()
    yield
    map_coverage._CACHE.clear()


def test_the_manifest_counts_the_pins_the_published_detail_archive_does_not_reach(
    logged_in_client, tiles_dir
):
    """A detail archive is cut to the regions that had pins WHEN IT WAS CUT; pins keep arriving.
    The count is what tells an operator the map has quietly stopped having streets where the
    household now takes photographs."""
    from sqlalchemy import create_engine, text

    from tests.test_map_markers import _wipe

    client, subject = logged_in_client
    (tiles_dir / DETAIL).write_bytes(ARCHIVE_BYTES)
    (tiles_dir / f"{DETAIL.removesuffix('.pmtiles')}.regions.geojson").write_text(
        json.dumps(REGIONS_DOC)
    )
    engine = create_engine(SYNC_URL)
    try:
        with engine.begin() as connection:
            owner_id = connection.execute(
                text("SELECT id FROM users WHERE subject = :s"), {"s": subject}
            ).scalar_one()
            memo_id = connection.execute(
                text(
                    "INSERT INTO memos"
                    " (uid, owner_id, content, visibility, pinned, created_at, updated_at)"
                    " VALUES ('memo-uncovered', :o, 'elsewhere', 'PUBLIC', false, now(), now())"
                    " RETURNING id"
                ),
                {"o": owner_id},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO attachments"
                    " (uid, owner_id, memo_id, filename, mime, size, status, storage_path,"
                    "  latitude, longitude, location_source)"
                    " VALUES ('faraway', :o, :m, 'f.jpg', 'image/jpeg', 1, 'ready', '/nowhere',"
                    "  12.5, 45.25, 'exif')"
                ),
                {"o": owner_id, "m": memo_id},
            )

        body = client.get("/api/map/manifest").json()
        assert body["detail"]["name"] == DETAIL
        assert body["uncovered"] == 1
    finally:
        engine.dispose()
        _wipe()


def test_with_no_detail_archive_the_coverage_question_is_answered_with_nothing(
    logged_in_client, tiles_dir
):
    client, _ = logged_in_client
    body = client.get("/api/map/manifest").json()

    assert body["detail"] is None
    assert body["uncovered"] is None


def test_an_empty_tiles_directory_reports_nothing_rather_than_failing(
    logged_in_client, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "TILES_DIR", str(tmp_path / "never-extracted"))
    body = client.get("/api/map/manifest").json()
    assert body == {
        "build": None,
        "assets_commit": None,
        "planet": None,
        "detail": None,
        "uncovered": None,
    }


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


# --- the extraction script's one refusal ------------------------------------------------------

EXTRACT_SH = Path(__file__).resolve().parent.parent / "scripts" / "tiles" / "extract.sh"


def test_a_published_archive_that_stops_verifying_is_refused_never_rewritten(tmp_path):
    """The script may not repair an archive under the name it was published as.

    A client caches that archive's directory against its URL and a file response reads size and
    timestamp separately from bytes, so new bytes under an old name hand somebody a map of where
    things are that no longer matches — and deleting the file instead takes the basemap away from
    whoever is mid-session on it. So the refusal has to point FORWARD, at a fresh build name, and
    the message is asserted here because it is the entire remedy an operator gets.

    The extraction tool is a dependency, stubbed so it reports the archive as bad without a
    network round-trip; the script's own control flow runs in full.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    published = tiles / "planet-20260916.pmtiles"
    published.write_bytes(b"not a valid archive any more")
    before = published.read_bytes()

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = verify ] && exit 1; done\nexit 0\n')
    stub.chmod(0o755)

    run = subprocess.run(
        ["bash", str(EXTRACT_SH), str(tiles), "--build", "20260916", "--dev", "small"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
    )

    assert run.returncode == 2, (run.returncode, run.stdout, run.stderr)
    assert "never rewritten or deleted" in run.stderr, run.stderr
    assert "--build <newer YYYYMMDD>" in run.stderr, run.stderr
    assert published.read_bytes() == before  # not repaired, not truncated, not touched
    assert list(tiles.glob("*.tmp")) == []  # and nothing half-written left behind


# --- the extraction script's pruning ----------------------------------------------------------

DOCKER_STUB = """#!/bin/sh
# Plays `docker run ... protomaps/go-pmtiles <subcommand>`: an extract writes the archive it was
# asked for, a verify accepts it. The tiles directory is learned from the -v mount the script
# passes, exactly as the real tool learns it, so a script that stopped mounting it would fail here.
tiles=""
for a in "$@"; do
  case "$a" in *:/tiles) tiles="${a%:/tiles}" ;; esac
done
mode=""
src=""
out=""
for a in "$@"; do
  if [ -z "$mode" ]; then
    case "$a" in extract|verify|show) mode="$a" ;; esac
    continue
  fi
  case "$a" in -*) continue ;; esac
  if [ "$mode" = extract ]; then
    if [ -z "$src" ]; then src="$a"; elif [ -z "$out" ]; then out="$a"; fi
  fi
done
if [ "$mode" = extract ]; then
  [ -n "$out" ] || exit 3
  [ -n "$tiles" ] || exit 3
  printf 'PMTiles-stub-archive' > "${tiles}/$(basename "$out")"
fi
exit 0
"""


def _stub_docker(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    return stub_dir


def _seed_archive(tiles: Path, name: str, when: int) -> Path:
    """One previously published archive, stamped with the mtime publication order is read from."""
    path = tiles / name
    path.write_bytes(b"a published archive")
    os.utime(path, (when, when))
    return path


def _run_extract(tiles: Path, stub_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(EXTRACT_SH), str(tiles), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
    )


def test_publishing_keeps_the_new_generation_and_the_one_it_replaced_and_no_more(tmp_path):
    """Two generations of each kind survive an extraction, and they are the right two.

    The previous generation has to stay: a client mid-session holds its directory and keeps
    range-reading that exact file, so deleting it in the run that replaces it breaks the map for
    whoever is looking at it. Everything older than that is unreachable — the manifest names only
    the newest — and keeping it would grow a multi-gigabyte directory without bound. The real
    script runs; only the extraction tool is stubbed.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)
    oldest_planet = _seed_archive(tiles, "planet-20260901.pmtiles", 1_000_000_000)
    older_planet = _seed_archive(tiles, "planet-20260908.pmtiles", 1_100_000_000)
    previous_planet = _seed_archive(tiles, "planet-20260915.pmtiles", 1_200_000_000)
    oldest_detail = _seed_archive(tiles, "detail-20260901-aaaaaaaa.pmtiles", 1_000_000_000)
    older_detail = _seed_archive(tiles, "detail-20260908-bbbbbbbb.pmtiles", 1_100_000_000)
    previous_detail = _seed_archive(tiles, "detail-20260915-cccccccc.pmtiles", 1_200_000_000)

    run = _run_extract(tiles, stub_dir, "--build", "20260917", "--dev", "small")
    assert run.returncode == 0, (run.stdout, run.stderr)

    published_planet = tiles / "planet-20260917.pmtiles"
    assert published_planet.is_file(), run.stdout  # positive control: something WAS published
    [published_detail] = [
        p
        for p in tiles.glob("detail-20260917-*.pmtiles")
        if p.name not in {oldest_detail.name, older_detail.name, previous_detail.name}
    ]

    assert sorted(p.name for p in tiles.glob("planet-*.pmtiles")) == sorted(
        [published_planet.name, previous_planet.name]
    )
    assert sorted(p.name for p in tiles.glob("detail-*.pmtiles")) == sorted(
        [published_detail.name, previous_detail.name]
    )
    for gone in (oldest_planet, older_planet, oldest_detail, older_detail):
        assert not gone.exists(), gone.name


def test_a_kind_with_nothing_to_supersede_loses_nothing(tmp_path):
    """The other branch: below the keep count, pruning removes nothing at all. Without this case a
    prune that deleted the previous generation on every run would still pass the case above."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)
    only_planet = _seed_archive(tiles, "planet-20260915.pmtiles", 1_200_000_000)
    only_detail = _seed_archive(tiles, "detail-20260915-cccccccc.pmtiles", 1_200_000_000)

    run = _run_extract(tiles, stub_dir, "--build", "20260917", "--dev", "small")
    assert run.returncode == 0, (run.stdout, run.stderr)

    assert only_planet.exists()
    assert only_detail.exists()
    assert len(list(tiles.glob("planet-*.pmtiles"))) == 2
    assert len(list(tiles.glob("detail-*.pmtiles"))) == 2
    assert "removing superseded" not in run.stdout


# --- the regions a detail archive covers ------------------------------------------------------

REGIONS_DOC = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"cells": 1},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[8.0, 47.0], [9.0, 47.0], [9.0, 48.0], [8.0, 48.0], [8.0, 47.0]]]
                ],
            },
        }
    ],
}


def _regions_file(tmp_path: Path) -> Path:
    path = tmp_path / "regions.geojson"
    path.write_text(json.dumps(REGIONS_DOC))
    return path


def test_the_regions_a_detail_archive_was_cut_from_are_published_under_its_own_stem(tmp_path):
    """The manifest names one detail archive; nothing on disk said which ground it covers.

    Published under the archive's immutable stem, so the two are one thing: the name that picks
    the archive picks its regions, and a regions file can never be paired with an archive cut from
    a different one.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = _regions_file(tmp_path)

    run = _run_extract(tiles, _stub_docker(tmp_path), str(regions), "--build", "20260917")
    assert run.returncode == 0, (run.stdout, run.stderr)

    [detail] = list(tiles.glob("detail-20260917-*.pmtiles"))
    sidecar = tiles / f"{detail.name.removesuffix('.pmtiles')}.regions.geojson"
    assert sidecar.is_file(), sorted(p.name for p in tiles.iterdir())
    assert json.loads(sidecar.read_text()) == REGIONS_DOC


def test_a_superseded_detail_archive_takes_its_regions_file_with_it(tmp_path):
    """The pair is one generation. A regions file left behind by the archive it describes would
    accumulate silently and, worse, could be read for an archive that is no longer there."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = _regions_file(tmp_path)
    kept, dropped = [], []
    for name, when in (
        ("detail-20260901-aaaaaaaa", 1_000_000_000),
        ("detail-20260908-bbbbbbbb", 1_100_000_000),
        ("detail-20260915-cccccccc", 1_200_000_000),
    ):
        _seed_archive(tiles, f"{name}.pmtiles", when)
        sidecar = tiles / f"{name}.regions.geojson"
        sidecar.write_text('{"type": "FeatureCollection", "features": []}')
        (kept if name.endswith("cccccccc") else dropped).append(sidecar)

    run = _run_extract(tiles, _stub_docker(tmp_path), str(regions), "--build", "20260917")
    assert run.returncode == 0, (run.stdout, run.stderr)

    assert len(list(tiles.glob("detail-*.pmtiles"))) == 2
    for survivor in kept:
        # The generation still being read keeps its regions.
        assert survivor.is_file(), survivor.name
    for gone in dropped:
        assert not gone.exists(), gone.name
    assert len(list(tiles.glob("*.regions.geojson"))) == 2
