"""How many visible pins fall outside the ground the current detail archive covers.

The basemap is published in two pieces: a world archive at low zoom and a detail archive cut to
the regions the pins were in WHEN IT WAS CUT. Pins keep arriving; the archive does not follow them
on its own. So a household that starts photographing somewhere new gets a map that silently stops
having streets on it, and nothing says so until someone looks. This module is that signal: a count
of the pins the detailed part does not reach, which an operator reads as "a re-extract is due".

Two properties are deliberate:

* **A count, never a place.** The answer is an integer and the log event carries the integer and
  the archive's name. A coordinate value never appears in a log line, an error message or an
  unauthenticated response — the whole reason the map is self-hosted — and the only way to keep
  that true of a diagnostic is for the diagnostic never to hold one.
* **The feed's visibility rule, borrowed rather than restated.** The points counted come from the
  markers query itself. A second rule here would drift from that one, and a drift in this
  direction would count — and, through the count, disclose the existence of — photos the caller
  may not see.

A pin exactly on a region boundary counts as covered: the regions are cells buffered around the
pins themselves, and calling a pin on the line a gap would report work that is already done.
"""

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog
from anyio import to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services import map_markers

logger = structlog.get_logger(__name__)

# One point per photo, so the query and the ray-casts are paid once per ten minutes rather than on
# every map open and every theme toggle — the manifest is fetched by both.
CACHE_TTL_S = 600.0

# Keyed by (viewer, detail archive): the visibility rule makes the answer the CALLER's, and a new
# extraction makes the previous answer meaningless rather than merely stale.
_CACHE: dict[tuple[int, str], tuple[float, int]] = {}

# The regions are lon/lat degrees; a point this close to an edge is on it. Small enough that no
# real pin is moved across a boundary by it, large enough to absorb the float arithmetic of the
# cross product below.
_ON_EDGE = 1e-9

Point = tuple[float, float]  # (latitude, longitude), the order the markers carry
Ring = list[list[float]]  # GeoJSON [longitude, latitude] pairs
Polygon = list[Ring]  # exterior ring first, holes after it


def _monotonic() -> float:
    """The cache's clock, named so a test can hold it still without sleeping."""
    return time.monotonic()


def _polygons(regions: Any) -> list[Polygon]:
    """Every polygon in a regions document, whichever GeoJSON wrapper it arrived in.

    The sidecar is what the backfill wrote (a FeatureCollection), while the extraction tool takes
    a bare Polygon or MultiPolygon, and the same file is handed to both. Anything else — a
    geometry with no coordinates, a document of another shape — yields no polygons, which reports
    everything as uncovered rather than claiming coverage nothing backs.
    """
    if not isinstance(regions, dict):
        return []
    kind = regions.get("type")
    if kind == "FeatureCollection":
        found: list[Polygon] = []
        for feature in regions.get("features") or []:
            found += _polygons(feature)
        return found
    if kind == "Feature":
        return _polygons(regions.get("geometry"))
    coordinates = regions.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    if kind == "Polygon":
        return [coordinates]
    if kind == "MultiPolygon":
        return [polygon for polygon in coordinates if isinstance(polygon, list)]
    return []


def _on_ring(latitude: float, longitude: float, ring: Ring) -> bool:
    """Whether the point sits ON the ring — the case a ray-cast answers arbitrarily."""
    for start, end in zip(ring, ring[1:], strict=False):
        (x1, y1), (x2, y2) = (start[0], start[1]), (end[0], end[1])
        cross = (x2 - x1) * (latitude - y1) - (y2 - y1) * (longitude - x1)
        if abs(cross) > _ON_EDGE:
            continue
        if (
            min(x1, x2) - _ON_EDGE <= longitude <= max(x1, x2) + _ON_EDGE
            and min(y1, y2) - _ON_EDGE <= latitude <= max(y1, y2) + _ON_EDGE
        ):
            return True
    return False


def _inside_ring(latitude: float, longitude: float, ring: Ring) -> bool:
    """The ray cast itself: a half-line east of the point crosses the ring an odd number of times.

    Each edge is counted only when it straddles the point's latitude, with one end strictly above
    and one not — which is what stops a vertex exactly at that latitude from being counted twice.
    """
    inside = False
    for start, end in zip(ring, ring[1:], strict=False):
        (x1, y1), (x2, y2) = (start[0], start[1]), (end[0], end[1])
        if (y1 > latitude) == (y2 > latitude):
            continue
        crossing = x1 + (latitude - y1) * (x2 - x1) / (y2 - y1)
        if longitude < crossing:
            inside = not inside
    return inside


def _covers(latitude: float, longitude: float, polygon: Polygon) -> bool:
    if not polygon:
        return False
    exterior, holes = polygon[0], polygon[1:]
    if _on_ring(latitude, longitude, exterior):
        return True
    if not _inside_ring(latitude, longitude, exterior):
        return False
    for hole in holes:
        if _on_ring(latitude, longitude, hole):
            return True  # the hole's edge is still the covered ring's edge
        if _inside_ring(latitude, longitude, hole):
            return False
    return True


def uncovered_count(points: Sequence[Point], regions: Any) -> int:
    """How many of `points` (latitude, longitude) no polygon of `regions` covers."""
    polygons = _polygons(regions)
    return sum(
        1
        for latitude, longitude in points
        if not any(_covers(latitude, longitude, polygon) for polygon in polygons)
    )


def regions_path(tiles_root: Path, detail_name: str) -> Path:
    """The regions file the extraction published beside that archive, under its own stem."""
    return tiles_root / f"{detail_name.removesuffix('.pmtiles')}.regions.geojson"


def _read_regions_sync(tiles_root: Path, detail_name: str) -> Any | None:
    try:
        return json.loads(regions_path(tiles_root, detail_name).read_text())
    except (OSError, ValueError):
        # An archive published before the sidecar existed, or a file being rewritten right now:
        # the question has no answer, and inventing one would report a gap that is not there.
        return None


async def uncovered_for(
    db: AsyncSession, user: User, tiles_root: Path, detail_name: str | None
) -> int | None:
    """The viewer's uncovered-pin count for the current detail archive, or None if unanswerable.

    None rather than 0 when nothing is published to compare against: 0 reads as "every pin is on
    the detailed map", which is the opposite of what an absent archive means.
    """
    if detail_name is None:
        return None
    key = (user.id, detail_name)
    now = _monotonic()
    cached = _CACHE.get(key)
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    regions = await to_thread.run_sync(_read_regions_sync, tiles_root, detail_name)
    if regions is None:
        return None
    rows = await map_markers.located_markers(db, user)
    points = [
        (att.latitude, att.longitude)
        for att, _memo_uid, _taken in rows
        if att.latitude is not None and att.longitude is not None
    ]
    count = uncovered_count(points, regions)
    _CACHE[key] = (now, count)
    if count > 0:
        # The count and the archive that is short of it — everything an operator needs to decide
        # to re-extract, and nothing that says where anybody was.
        logger.info("map.coverage_gap", count=count, detail=detail_name)
    return count
