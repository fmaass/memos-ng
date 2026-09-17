"""The self-hosted basemap: the vector-tile archives and the fonts and sprites the style needs.

Nothing here talks to a third party, which is the point: a map view otherwise announces every
place the household has been to whoever hosts the tiles. The archives are produced offline by
`scripts/tiles/extract.sh` and served straight off disk from here.

Three things about the shape of these routes are deliberate:

* **The file name IS the version.** A PMTiles client caches an archive's header and directory per
  URL, and a directory that says "this tile is absent" is never re-fetched — so republishing under
  the same name would pair a stale directory with new bytes and leave holes on the map that only a
  cache wipe clears. Every extraction therefore publishes a new, never-reused name, the manifest
  points at it, and the previous file stays on disk for clients still reading it.
* **The transfer holds no database connection.** Authentication is a short-lived session opened
  and closed before the response object is built, not a yield-based dependency: an archive is
  gigabytes, and a handful of concurrent downloads holding pool connections for their duration is
  how the shared instance runs out of them.
* **The existence check precedes the response.** Starlette's `FileResponse` stats the path while
  it is already sending, so a missing file there raises inside the streaming layer and surfaces as
  a 500. Checking first makes an absent archive the 404 it actually is.
"""

import mimetypes
import re
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import auth
from app.database import async_session
from app.services.asset_paths import tiles_dir

router = APIRouter(prefix="/api/map", tags=["map"])

# The only names this route will serve. `planet-<build>` covers the whole world at low zoom;
# `detail-<build>-<spec>` covers the regions that have pins, at street zoom. Both version parts are
# hex-and-dashes, so the pattern admits exactly what the extraction script publishes and nothing
# that could name another file on disk.
ARCHIVE_NAME = re.compile(r"^(planet|detail)-[0-9a-f-]+\.pmtiles$")

# A week, and `immutable` on top of it: these URLs are never reused for different bytes, so a
# client that has one has it for good. `private` because every response here is scoped to one
# authenticated viewer and must not be stored by a shared cache.
TILE_CACHE_CONTROL = "private, max-age=604800, immutable"

ARCHIVE_MEDIA_TYPE = "application/octet-stream"
# Glyph ranges are protocol buffers; `mimetypes` has no entry for the extension and would fall back
# to octet-stream, which some clients refuse to parse.
GLYPH_MEDIA_TYPE = "application/x-protobuf"


class MapArchive(BaseModel):
    """One published basemap archive."""

    name: str
    url: str
    size: int


class MapManifest(BaseModel):
    """Which basemap archives are available, and the date of the data they were cut from.

    `detail` is null when no region archive has been published; a client renders the world archive
    alone rather than nothing at all.
    """

    build: str | None
    planet: MapArchive | None
    detail: MapArchive | None


def _archive_path_sync(name: str) -> Path | None:
    path = tiles_dir() / name
    return path if path.is_file() else None


def _asset_media_type(path: Path) -> str:
    if path.suffix == ".pbf":
        return GLYPH_MEDIA_TYPE
    return mimetypes.guess_type(path.name)[0] or ARCHIVE_MEDIA_TYPE


def _asset_path_sync(relative: str) -> tuple[Path, str] | None:
    """Resolve a style-asset request inside the served directory, or refuse it.

    Containment is decided on the RESOLVED path, never on the spelling: `..`, its percent-encoded
    forms and an absolute path all collapse to something outside the base directory here, and
    `is_relative_to` is what notices. Dot-prefixed components are refused separately — nothing the
    style needs begins with one, and serving them would hand out whatever a stray editor or tool
    left behind.
    """
    base = (tiles_dir() / "assets").resolve()
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base):
        return None
    if any(part.startswith(".") for part in candidate.relative_to(base).parts):
        return None
    if not candidate.is_file():
        return None
    return candidate, _asset_media_type(candidate)


def _newest_archive(entries: list[Path], kind: str) -> dict[str, Any] | None:
    """The current generation of one archive kind.

    An extraction keeps the file it replaces, so more than one can be on disk; the current one is
    the most recently published, and publication is a rename, which is what the modification time
    records. Reading the directory rather than a recorded name means the manifest can only ever
    point at a file that is really there.
    """
    candidates = [
        p for p in entries if p.name.startswith(f"{kind}-") and ARCHIVE_NAME.fullmatch(p.name)
    ]
    if not candidates:
        return None
    stats = {p: p.stat() for p in candidates}
    newest = max(candidates, key=lambda p: (stats[p].st_mtime_ns, p.name))
    return {
        "name": newest.name,
        "url": f"/api/map/tiles/{newest.name}",
        "size": stats[newest].st_size,
    }


def _build_date(root: Path) -> str | None:
    try:
        text = (root / "BUILD").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "build":
            return value.strip() or None
    return None


def _manifest_sync() -> dict[str, Any]:
    root = tiles_dir()
    try:
        entries = [p for p in root.iterdir() if p.is_file()]
    except OSError:  # never extracted on this host yet — an empty manifest, not an error
        entries = []
    return {
        "build": _build_date(root),
        "planet": _newest_archive(entries, "planet"),
        "detail": _newest_archive(entries, "detail"),
    }


async def _authenticate(request: Request) -> None:
    # The session is opened and closed here, before anything starts streaming — see the module
    # docstring on connection lifetime.
    async with async_session() as db:
        await auth.require_user(request, db)


@router.get("/manifest", response_model=MapManifest)
async def basemap_manifest(request: Request, response: Response) -> MapManifest:
    """List the basemap archives this server currently offers.

    Never cached: it is the pointer that changes when new map data is published, and a client
    holding a stale copy would keep asking for an archive that has been superseded.
    """
    await _authenticate(request)
    response.headers["Cache-Control"] = "no-store"
    return MapManifest(**await to_thread.run_sync(_manifest_sync))


@router.get("/tiles/{name}")
@router.head("/tiles/{name}")
async def serve_basemap_archive(name: str, request: Request) -> FileResponse:
    """Serve one basemap archive, by byte range.

    A map client fetches a few kilobytes per tile out of a multi-gigabyte file, so a ranged request
    is the normal case: it answers 206 and states the range it served, a range that starts past the
    end of the archive answers 416, and a request without a range header answers the whole file.
    """
    await _authenticate(request)
    if not ARCHIVE_NAME.fullmatch(name):
        raise HTTPException(status_code=404, detail="Unknown archive")
    path = await to_thread.run_sync(_archive_path_sync, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    return FileResponse(
        path,
        media_type=ARCHIVE_MEDIA_TYPE,
        headers={"Cache-Control": TILE_CACHE_CONTROL},
    )


@router.get("/assets/{path:path}")
async def serve_basemap_asset(path: str, request: Request) -> FileResponse:
    """Serve one map style asset — a glyph range or a sprite sheet."""
    await _authenticate(request)
    resolved = await to_thread.run_sync(_asset_path_sync, path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(
        resolved[0],
        media_type=resolved[1],
        headers={"Cache-Control": TILE_CACHE_CONTROL},
    )
