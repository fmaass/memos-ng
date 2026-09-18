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
# Plays `docker run ...` for both images extract.sh uses: the go-pmtiles extractor (an extract
# writes the archive it was asked for, a verify accepts it) and the small helper that performs the
# renames as the publishing user (`sh -c <snippet>` against /tiles). The tiles directory is learned
# from the -v mount the script passes, exactly as the real tools learn it, so a script that stopped
# mounting it would fail here.
#
# With PMTILES_STUB_ARGV set it appends its own argv to that file, one argument per line and a `--`
# between invocations: the only way to see what the script actually handed `docker run`.
#
# With PMTILES_STUB_GUARD set to a mode, it stands in for a container running as a user the caller
# is NOT: the tiles directory is writable only while this stub is working, and goes back to that
# mode afterwards. A write the script makes outside a container then fails, which is the whole
# point of the read-only case below.
#
# With PMTILES_STUB_NO_IMAGE set, `docker image inspect` fails: the helper image is not on this
# machine.
tiles=""
src=""
for a in "$@"; do
  case "$a" in
    *:/tiles) tiles="${a%:/tiles}" ;;
    *:/src:ro) src="${a%:/src:ro}" ;;
  esac
done
if [ -n "${PMTILES_STUB_ARGV:-}" ]; then
  for a in "$@"; do printf '%s\n' "$a" >> "$PMTILES_STUB_ARGV"; done
  printf -- '--\n' >> "$PMTILES_STUB_ARGV"
fi
lift() { [ -n "${PMTILES_STUB_GUARD:-}" ] && [ -n "$tiles" ] && chmod u+w "$tiles"; return 0; }
drop() {
  [ -n "${PMTILES_STUB_GUARD:-}" ] && [ -n "$tiles" ] && chmod "$PMTILES_STUB_GUARD" "$tiles"
  return 0
}

if [ "${1:-}" = image ] && [ "${2:-}" = inspect ]; then
  [ -z "${PMTILES_STUB_NO_IMAGE:-}" ] || exit 1
  exit 0
fi

# The helper container. The script must hand docker `--entrypoint sh` and then `-c <snippet> sh
# <args...>`, so the snippet is the command rather than the arguments of whatever ENTRYPOINT the
# image happens to carry. Run it exactly as the container would: the mount points come in as T and
# S, the names as positional parameters, and nothing is rewritten textually.
case " $* " in
  *" --entrypoint sh "*)
    while [ "$#" -gt 0 ] && [ "$1" != "-c" ]; do shift; done
    shift
    snippet="$1"; shift
    shift            # the $0 the script passes for the snippet's own name
    lift
    T="$tiles" S="$src" sh -c "$snippet" sh "$@"
    rc=$?
    drop
    exit $rc
    ;;
esac

mode=""
out=""
seen_src=""
for a in "$@"; do
  if [ -z "$mode" ]; then
    case "$a" in extract|verify|show) mode="$a" ;; esac
    continue
  fi
  case "$a" in -*) continue ;; esac
  if [ "$mode" = extract ]; then
    if [ -z "$seen_src" ]; then seen_src="$a"; elif [ -z "$out" ]; then out="$a"; fi
  fi
done
if [ "$mode" = extract ]; then
  [ -n "$out" ] || exit 3
  [ -n "$tiles" ] || exit 3
  lift
  printf 'PMTiles-stub-archive' > "${tiles}/$(basename "$out")"
  rc=$?
  drop
  [ "$rc" = 0 ] || exit "$rc"
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


def _run_extract(
    tiles: Path,
    stub_dir: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(EXTRACT_SH), str(tiles), *args],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{stub_dir}:{os.environ['PATH']}",
            **(extra_env or {}),
        },
    )


def _recorded_invocations(argv_log: Path) -> list[list[str]]:
    """The stub's argv log, split back into one list per `docker run` the script issued."""
    calls: list[list[str]] = []
    current: list[str] = []
    for line in argv_log.read_text().splitlines():
        if line == "--":
            calls.append(current)
            current = []
        else:
            current.append(line)
    return calls


def _user_argument(argv: list[str]) -> str | None:
    """The value docker would act on: the LAST --user, since a later one wins."""
    found = None
    for flag, value in zip(argv, argv[1:], strict=False):
        if flag == "--user":
            found = value
    return found


# --- the extraction script's container user (MEMNG-353) ---------------------------------------


def test_the_container_user_defaults_to_the_caller_so_a_local_directory_is_unchanged(tmp_path):
    """No --user: every container still runs as the caller, which is what keeps the archives owned
    by whoever ran the script on an ordinary local directory. This is the control for the override
    test below — without it, a change that hard-coded the production share's uid would still pass
    that one while silently breaking every developer's extract."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)
    argv_log = tmp_path / "argv.log"

    run = _run_extract(
        tiles,
        stub_dir,
        "--build",
        "20260917",
        "--dev",
        "small",
        extra_env={"PMTILES_STUB_ARGV": str(argv_log)},
    )
    assert run.returncode == 0, (run.stdout, run.stderr)

    calls = _recorded_invocations(argv_log)
    assert len(calls) == 4, calls  # planet extract+verify, detail extract+verify
    assert {_user_argument(c) for c in calls} == {f"{os.getuid()}:{os.getgid()}"}


# 1027:100 is the production share's owner and the value RELEASING.md names, but a runner that
# happened to BE 1027:100 would make "the override differs from the caller" vacuously false, so the
# override these tests use is chosen to differ from whoever runs them.
PRODUCTION_USER = "1027:100"
OVERRIDE_USER = PRODUCTION_USER if (os.getuid(), os.getgid()) != (1027, 100) else "1028:101"


def test_the_container_user_override_reaches_every_container_invocation(tmp_path):
    """--user has to reach EVERY container, not just the extractor.

    On the production tiles share only the share's owner may write, and this script writes there in
    more than one way: the extraction, then the renames that publish it. A test that only checked
    the script's exit code would pass with the flag parsed and dropped, which is the bug; hence a
    stub `docker` on PATH that records its own argv, and an assertion over every invocation — one
    call left at the caller's uid is one permission denied, mid-publish.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)
    argv_log = tmp_path / "argv.log"

    run = _run_extract(
        tiles,
        stub_dir,
        "--build",
        "20260917",
        "--dev",
        "small",
        "--user",
        OVERRIDE_USER,
        extra_env={"PMTILES_STUB_ARGV": str(argv_log)},
    )
    assert run.returncode == 0, (run.stdout, run.stderr)

    calls = _recorded_invocations(argv_log)
    # the extractor's four, plus every rename/delete/write the publish performs
    assert len(calls) > 4, calls
    users = {_user_argument(c) for c in calls if any(a.endswith(":/tiles") for a in c)}
    assert users == {OVERRIDE_USER}, calls
    # And the helper is invoked as a command, not as arguments to an ENTRYPOINT the image may
    # carry: `--entrypoint sh <image> -c <snippet> sh <args…>`. Any image with a POSIX shell is
    # documented as usable, and this is the shape that makes that true.
    helpers = [c for c in calls if "--entrypoint" in c]
    assert helpers, calls
    for call in helpers:
        at = call.index("--entrypoint")
        assert call[at + 1 : at + 4] == ["sh", "busybox:stable", "-c"], call
    assert not any(f"{os.getuid()}:{os.getgid()}" in c for c in calls), calls
    # positive control: the assertions ran against a real end-to-end publish
    assert (tiles / "planet-20260917.pmtiles").is_file(), run.stdout
    assert list(tiles.glob("detail-20260917-*.pmtiles")), run.stdout


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the directory mode this case relies on")
def test_with_an_override_the_publish_touches_a_directory_the_caller_cannot_write(tmp_path):
    """The real surface: a tiles directory the CALLER may not write, published into anyway.

    This is what the production share is. The first version of this change made only the extractor
    run as the share's owner, so the extract succeeded and the very next step — the rename that
    publishes it — was refused by the same directory; the script still could not publish, which was
    the ticket. Here the directory is mode 0555 and the stub `docker`, standing in for a container
    running as a user the caller is not, lifts the mode only while it is itself working. So every
    byte that lands in this directory provably came from a container: anything the script does in
    its own shell fails.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = tmp_path / "regions.geojson"
    regions.write_text('{"type":"Polygon","coordinates":[]}')
    stub_dir = _stub_docker(tmp_path)
    tiles.chmod(0o555)
    try:
        run = _run_extract(
            tiles,
            stub_dir,
            str(regions),
            "--build",
            "20260917",
            "--user",
            OVERRIDE_USER,
            extra_env={"PMTILES_STUB_GUARD": "0555"},
        )
        assert run.returncode == 0, (run.returncode, run.stdout, run.stderr)

        names = sorted(p.name for p in tiles.iterdir())
        assert "planet-20260917.pmtiles" in names, names
        detail = [n for n in names if n.startswith("detail-20260917-") and n.endswith(".pmtiles")]
        assert detail, names
        assert any(n.endswith(".regions.geojson") for n in names), names  # the sidecar
        assert "BUILD" in names, names
        assert not [n for n in names if n.endswith(".tmp")], names  # nothing half-written left
        build = (tiles / "BUILD").read_text()
        assert "planet=planet-20260917.pmtiles" in build, build
    finally:
        tiles.chmod(0o755)


def test_a_missing_helper_image_is_refused_before_anything_is_written(tmp_path):
    """No pull at publish time, so an absent helper image has to be a refusal up front.

    Discovered halfway through, it surfaces between an extracted `.tmp` and the rename that would
    publish it — the one moment this script has no good answer for, and one that costs a multi-
    gigabyte download to reach. The message has to name the image and how to get it.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    (tiles / "BUILD").write_text("build=20260101\n")  # something already published here
    before = sorted(p.name for p in tiles.iterdir())
    stub_dir = _stub_docker(tmp_path)

    run = _run_extract(
        tiles,
        stub_dir,
        "--build",
        "20260917",
        "--dev",
        "small",
        "--user",
        OVERRIDE_USER,
        extra_env={"PMTILES_STUB_NO_IMAGE": "1"},
    )

    assert run.returncode == 2, (run.returncode, run.stdout, run.stderr)
    assert "busybox:stable" in run.stderr, run.stderr
    assert "docker pull busybox:stable" in run.stderr, run.stderr
    assert sorted(p.name for p in tiles.iterdir()) == before


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the directory mode this case relies on")
def test_an_override_publish_prunes_and_overwrites_through_the_container_too(tmp_path):
    """The read-only case above starts from an empty directory, so it never exercises the writes
    that only happen when something is already there: removing a stale `.tmp`, pruning the
    generation that has fallen out of the window, and replacing BUILD. On the production share
    every one of those is a write the caller is refused, and each is a separate call site.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    oldest = _seed_archive(tiles, "planet-20260101.pmtiles", 1_700_000_000)
    _seed_archive(tiles, "planet-20260201.pmtiles", 1_700_100_000)
    oldest_detail = _seed_archive(tiles, "detail-20260101-aaaaaaaa.pmtiles", 1_700_000_000)
    oldest_sidecar = tiles / "detail-20260101-aaaaaaaa.regions.geojson"
    oldest_sidecar.write_text('{"type":"Polygon","coordinates":[]}')
    kept_detail = _seed_archive(tiles, "detail-20260201-bbbbbbbb.pmtiles", 1_700_100_000)
    stale = tiles / "planet-20260917.pmtiles.tmp"
    stale.write_bytes(b"half an archive from a run that died")
    (tiles / "BUILD").write_text("build=20260201\nassets_rev=deadbeef\n")
    regions = tmp_path / "regions.geojson"
    regions.write_text('{"type":"Polygon","coordinates":[]}')
    stub_dir = _stub_docker(tmp_path)

    tiles.chmod(0o555)
    try:
        run = _run_extract(
            tiles,
            stub_dir,
            str(regions),
            "--build",
            "20260917",
            "--user",
            OVERRIDE_USER,
            extra_env={"PMTILES_STUB_GUARD": "0555"},
        )
        assert run.returncode == 0, (run.returncode, run.stdout, run.stderr)

        names = sorted(p.name for p in tiles.iterdir())
        assert not stale.exists(), names  # the stale .tmp was removed, from inside the container
        assert not oldest.exists(), names  # pruned
        assert not oldest_detail.exists(), names
        assert not oldest_sidecar.exists(), names  # a generation's regions file goes with it
        assert kept_detail.exists(), names  # the one it replaced is still being read
        assert "planet-20260917.pmtiles" in names, names
        assert not [n for n in names if n.endswith(".tmp")], names
        build = (tiles / "BUILD").read_text()
        assert "planet=planet-20260917.pmtiles" in build, build
        assert "detail=detail-20260917-" in build, build
        assert "assets_rev=deadbeef" in build, build  # the assets record survives a re-extract
    finally:
        tiles.chmod(0o755)


def test_the_user_flag_without_a_value_is_refused(tmp_path):
    """`--user` last on the line: the value is missing, and consuming the next argument that is not
    there must not silently become "no override" on a share where that is the difference between
    publishing and a permission denied."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)

    run = _run_extract(tiles, stub_dir, "--build", "20260917", "--dev", "small", "--user")

    assert run.returncode == 2, (run.returncode, run.stdout, run.stderr)
    assert "--user needs a value" in run.stderr, run.stderr
    assert list(tiles.iterdir()) == [], list(tiles.iterdir())


def test_an_exported_pmtiles_user_does_not_change_the_container_user(tmp_path):
    """Which uid wrote a published archive belongs in the command an operator can read back, not in
    an exported variable that a stale shell can carry into the next publish unseen. The name is an
    internal one; a value in the environment has to be ignored."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)
    argv_log = tmp_path / "argv.log"

    run = _run_extract(
        tiles,
        stub_dir,
        "--build",
        "20260917",
        "--dev",
        "small",
        extra_env={"PMTILES_USER": "31337:31337", "PMTILES_STUB_ARGV": str(argv_log)},
    )
    assert run.returncode == 0, (run.stdout, run.stderr)

    calls = _recorded_invocations(argv_log)
    assert {_user_argument(c) for c in calls} == {f"{os.getuid()}:{os.getgid()}"}, calls
    assert not any("31337:31337" in c for c in calls), calls


@pytest.mark.parametrize("value", ["root", "1027", "1027:100:1", "1027:staff", ""])
def test_a_container_user_that_is_not_numeric_uid_gid_is_refused_before_anything_runs(
    tmp_path, value
):
    """The share's ACL matches a numeric uid; the go-pmtiles image carries no user database, so a
    name resolves to nothing inside it. Both failures surface late and opaquely — a permission
    denied halfway through a multi-gigabyte extract — so the shape is checked at parse time, before
    any container starts."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    stub_dir = _stub_docker(tmp_path)
    argv_log = tmp_path / "argv.log"

    run = _run_extract(
        tiles,
        stub_dir,
        "--build",
        "20260917",
        "--dev",
        "small",
        "--user",
        value,
        extra_env={"PMTILES_STUB_ARGV": str(argv_log)},
    )

    assert run.returncode == 2, (run.returncode, run.stdout, run.stderr)
    assert "--user must be a numeric uid:gid" in run.stderr, run.stderr
    assert not argv_log.exists(), "a container ran before the value was rejected"
    assert list(tiles.iterdir()) == [], list(tiles.iterdir())


def test_a_publish_survives_a_tiles_directory_whose_path_is_awkward(tmp_path):
    """No `--user`, i.e. the path every developer takes. The directories are not shell text.

    A tiles directory with a space in it, and a component spelled like the mount point the helper
    uses for the regions source (`/src`, as in a checkout under ~/src): a publisher that pastes
    paths into a shell snippet, or rewrites container paths back textually, corrupts one or the
    other. Both spellings are ordinary on a developer's machine.
    """
    tiles = tmp_path / "my src" / "src" / "tiles"
    tiles.mkdir(parents=True)
    regions = tmp_path / "regions.geojson"
    regions.write_text('{"type":"Polygon","coordinates":[]}')
    stub_dir = _stub_docker(tmp_path)

    run = _run_extract(tiles, stub_dir, str(regions), "--build", "20260917")

    assert run.returncode == 0, (run.returncode, run.stdout, run.stderr)
    names = sorted(p.name for p in tiles.iterdir())
    assert "planet-20260917.pmtiles" in names, names
    assert [n for n in names if n.startswith("detail-") and n.endswith(".pmtiles")], names
    assert [n for n in names if n.endswith(".regions.geojson")], names
    assert "BUILD" in names, names
    assert not [n for n in names if n.endswith(".tmp")], names


def test_a_publish_survives_a_regions_filename_that_is_awkward(tmp_path):
    """Same class, the other input: the sidecar is copied under the regions file's own basename.

    A quote in it ends the quoting of any snippet the name is pasted into, and a space splits it
    into two arguments. The sidecar has to arrive whole under the archive's stem either way.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = tmp_path / "it's regions.geojson"
    regions.write_text('{"type":"Polygon","coordinates":[]}')
    stub_dir = _stub_docker(tmp_path)

    run = _run_extract(tiles, stub_dir, str(regions), "--build", "20260917")

    assert run.returncode == 0, (run.returncode, run.stdout, run.stderr)
    sidecars = [p for p in tiles.iterdir() if p.name.endswith(".regions.geojson")]
    assert len(sidecars) == 1, sorted(p.name for p in tiles.iterdir())
    assert sidecars[0].read_text() == regions.read_text()
    assert sidecars[0].name.startswith("detail-20260917-")


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


# --- the archive and its regions become visible together ---------------------------------------

# `mv` is what makes a generation visible, so the moment to check is the rename itself. This stands
# in for it on PATH: an archive arriving without its regions file beside it stops the script right
# there, and every rename is recorded so a case can prove the check was reached at all.
MV_GUARD = """#!/bin/sh
dest=""
for a in "$@"; do
  case "$a" in -*) ;; *) src="$dest"; dest="$a" ;; esac
done
case "$dest" in
  */detail-*.pmtiles)
    echo "mv $dest" >> "$MV_LOG"
    sidecar="${dest%.pmtiles}.regions.geojson"
    if [ ! -f "$sidecar" ]; then
      echo "ORDERING VIOLATION: ${sidecar##*/} absent when ${dest##*/} appeared" >&2
      exit 9
    fi
    ;;
  *.pmtiles) echo "mv $dest" >> "$MV_LOG" ;;
esac
exec /bin/mv "$@"
"""

# An extraction tool that produces the archive and then makes the regions file unreadable: the
# publication is past the point of no return when copying the sidecar starts to fail.
DOCKER_STUB_POISONS_REGIONS = DOCKER_STUB.replace(
    '  printf \'PMTiles-stub-archive\' > "${tiles}/$(basename "$out")"\n',
    '  printf \'PMTiles-stub-archive\' > "${tiles}/$(basename "$out")"\n'
    '  [ -n "$POISON_REGIONS" ] && chmod 000 "$POISON_REGIONS"\n',
)

# Verification refuses the detail archive alone: the planet publishes normally, so the case is
# about what the FAILED publication leaves behind rather than about stopping early.
DOCKER_STUB_DETAIL_FAILS_VERIFY = DOCKER_STUB.replace(
    'if [ "$mode" = extract ]; then',
    'if [ "$mode" = verify ]; then\n'
    '  for a in "$@"; do\n'
    '    case "$a" in */detail-*.tmp) exit 1 ;; esac\n'
    "  done\n"
    "fi\n"
    'if [ "$mode" = extract ]; then',
    1,
)


def _stub_docker_variant(tmp_path: Path, body: str) -> Path:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(body)
    stub.chmod(0o755)
    return stub_dir


def test_a_detail_archive_never_becomes_visible_before_its_regions_file(tmp_path):
    """The pair is what has to be atomic, not each rename on its own.

    The manifest picks the newest detail archive and the coverage answer is read from the regions
    file under its stem. An archive that is visible first — for an instant, or for good after an
    interrupted run — is a generation whose regions do not exist, and nothing later repairs it,
    because a published name is never rewritten. So the regions file goes down first: one beside
    an archive nobody can see yet is harmless, the other way round is not.
    """
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = _regions_file(tmp_path)
    stub_dir = _stub_docker(tmp_path)
    (stub_dir / "mv").write_text(MV_GUARD)
    (stub_dir / "mv").chmod(0o755)
    log = tmp_path / "mv.log"

    run = subprocess.run(
        ["bash", str(EXTRACT_SH), str(tiles), str(regions), "--build", "20260917"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}", "MV_LOG": str(log)},
    )

    assert "ORDERING VIOLATION" not in run.stderr, run.stderr
    assert run.returncode == 0, (run.stdout, run.stderr)
    [detail] = list(tiles.glob("detail-20260917-*.pmtiles"))
    # Positive control: the guard DID run over the rename that publishes the detail archive.
    assert f"mv {detail}" in log.read_text(), log.read_text()
    assert (tiles / f"{detail.name.removesuffix('.pmtiles')}.regions.geojson").is_file()


def test_a_regions_file_that_cannot_be_copied_publishes_no_archive_at_all(tmp_path):
    """The failure the ordering exists for: if the sidecar cannot be written, the generation it
    belongs to must simply not appear. Half of it appearing is the state nothing can repair."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = _regions_file(tmp_path)
    stub_dir = _stub_docker_variant(tmp_path, DOCKER_STUB_POISONS_REGIONS)

    try:
        run = subprocess.run(
            ["bash", str(EXTRACT_SH), str(tiles), str(regions), "--build", "20260917"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{stub_dir}:{os.environ['PATH']}",
                "POISON_REGIONS": str(regions),
            },
        )

        assert run.returncode != 0, (run.stdout, run.stderr)
        assert list(tiles.glob("detail-*.pmtiles")) == [], sorted(p.name for p in tiles.iterdir())
    finally:
        regions.chmod(0o644)


def test_an_archive_that_fails_verification_leaves_no_regions_file_behind(tmp_path):
    """No orphan either: a regions file whose archive was never published would be pruned by
    nothing (prune follows archives) and would sit there describing a generation that does not
    exist."""
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    regions = _regions_file(tmp_path)
    stub_dir = _stub_docker_variant(tmp_path, DOCKER_STUB_DETAIL_FAILS_VERIFY)

    run = subprocess.run(
        ["bash", str(EXTRACT_SH), str(tiles), str(regions), "--build", "20260917"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
    )

    assert run.returncode != 0, (run.stdout, run.stderr)
    assert (tiles / "planet-20260917.pmtiles").is_file(), run.stdout  # control: it got that far
    assert list(tiles.glob("detail-*.pmtiles")) == []
    assert list(tiles.glob("*.regions.geojson")) == [], sorted(p.name for p in tiles.iterdir())
