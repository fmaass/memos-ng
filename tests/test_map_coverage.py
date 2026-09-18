"""How many pins the detailed part of the basemap does not reach — and what may be said about it.

The regions file is a ring of lon/lat pairs and the question is a point-in-polygon one, so the
cases here are the ones a ray-cast gets wrong: a point on an edge, a point on a vertex, a point
inside the bounding box of a multi-polygon but in neither of its parts, a hole. A pin exactly on
the boundary counts as COVERED: the regions are buffered cells drawn around the pins themselves,
and answering "uncovered" for a pin sitting on the line would report a gap that is not there.

The second contract is a privacy one (ADR-0018 draft): a coordinate value never appears in a log
line. This module's whole output is a COUNT, and the log event that reports it names the count and
the archive — asserted here against the rendered log, with a positive control proving the search
would have found a coordinate had one been there.
"""

import json
from pathlib import Path

import pytest
from app.services import map_coverage
from structlog.testing import capture_logs

from tests.test_map_markers import _located, _member, _memo

DETAIL = "detail-20260917-a1b2c3d4.pmtiles"

# lon 8..9, lat 47..48 — one axis-aligned cell of the kind the backfill's regions file is made of.
CELL = [[[8.0, 47.0], [9.0, 47.0], [9.0, 48.0], [8.0, 48.0], [8.0, 47.0]]]
FAR_CELL = [[[20.0, 10.0], [21.0, 10.0], [21.0, 11.0], [20.0, 11.0], [20.0, 10.0]]]
# The same cell with its middle cut out: a ring inside the exterior is a HOLE, not a second area.
HOLED_CELL = [
    [[8.0, 47.0], [9.0, 47.0], [9.0, 48.0], [8.0, 48.0], [8.0, 47.0]],
    [[8.4, 47.4], [8.6, 47.4], [8.6, 47.6], [8.4, 47.6], [8.4, 47.4]],
]


def _regions(*polygons: list[list[list[float]]]) -> dict:
    """The regions document as the backfill writes it: one feature, one MultiPolygon."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"cells": len(polygons)},
                "geometry": {"type": "MultiPolygon", "coordinates": list(polygons)},
            }
        ],
    }


# --- the pure count ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "point", "expected"),
    [
        ("inside the cell", (47.5, 8.5), 0),
        ("outside it", (47.5, 10.5), 1),
        ("north of it", (48.5, 8.5), 1),
        ("on the southern edge", (47.0, 8.5), 0),
        ("on the western edge", (47.5, 8.0), 0),
        ("on the south-west vertex", (47.0, 8.0), 0),
        ("on the north-east vertex", (48.0, 9.0), 0),
    ],
)
def test_one_point_against_one_cell(case, point, expected):
    assert map_coverage.uncovered_count([point], _regions(CELL)) == expected, case


def test_a_point_in_the_gap_between_two_cells_is_uncovered():
    """Inside the bounding box of the pair, inside neither of them — the case a bbox check passes
    and a real point-in-polygon has to refuse."""
    regions = _regions(CELL, FAR_CELL)

    assert map_coverage.uncovered_count([(47.5, 8.5)], regions) == 0
    assert map_coverage.uncovered_count([(10.5, 20.5)], regions) == 0
    assert map_coverage.uncovered_count([(30.0, 15.0)], regions) == 1


def test_a_point_in_a_hole_is_not_covered_by_the_ring_around_it():
    regions = _regions(HOLED_CELL)

    assert map_coverage.uncovered_count([(47.5, 8.5)], regions) == 1  # in the hole
    assert map_coverage.uncovered_count([(47.2, 8.2)], regions) == 0  # in the ring around it
    assert map_coverage.uncovered_count([(47.4, 8.5)], regions) == 0  # on the hole's own edge


def test_regions_with_no_polygons_at_all_leave_every_pin_uncovered():
    """An empty regions document covers nothing, so every pin is a gap. The safe direction: the
    alternative reports full coverage for a map that has none."""
    assert map_coverage.uncovered_count([(47.5, 8.5), (0.0, 0.0)], _regions()) == 2
    assert map_coverage.uncovered_count([], _regions(CELL)) == 0


def test_the_count_is_of_the_uncovered_points_alone():
    points = [(47.5, 8.5), (47.9, 8.9), (0.0, 0.0), (10.5, 20.5)]

    assert map_coverage.uncovered_count(points, _regions(CELL, FAR_CELL)) == 1


def test_the_geometry_is_read_out_of_either_wrapper_it_arrives_in():
    """The sidecar is what the backfill wrote — a FeatureCollection — but a bare geometry is what
    the extraction tool itself accepts, and an operator may well hand the same file to both."""
    bare = {"type": "MultiPolygon", "coordinates": [CELL]}
    single = {"type": "Polygon", "coordinates": CELL}

    assert map_coverage.uncovered_count([(47.5, 8.5), (0.0, 0.0)], bare) == 1
    assert map_coverage.uncovered_count([(47.5, 8.5), (0.0, 0.0)], single) == 1
    assert map_coverage.uncovered_count([(47.5, 8.5)], {"type": "MultiPolygon"}) == 1


# --- the manifest's answer, cached ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _empty_cache():
    map_coverage._CACHE.clear()
    yield
    map_coverage._CACHE.clear()


@pytest.fixture
def tiles_with_regions(tmp_path) -> Path:
    root = tmp_path / "tiles"
    root.mkdir()
    (root / f"{DETAIL.removesuffix('.pmtiles')}.regions.geojson").write_text(
        json.dumps(_regions(CELL))
    )
    return root


async def test_without_a_detail_archive_the_question_has_no_answer(adb, tiles_with_regions):
    """Null, not zero: nothing is published to compare pins against, and zero would read as
    "every pin is covered" — the opposite of the truth."""
    owner = await _member(adb, "coverage-none")

    assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, None) is None


async def test_a_detail_archive_whose_regions_file_is_missing_answers_nothing(adb, tmp_path):
    owner = await _member(adb, "coverage-missing")

    assert await map_coverage.uncovered_for(adb, owner, tmp_path, DETAIL) is None


async def test_the_count_is_reused_for_ten_minutes_and_recomputed_after(
    adb, tiles_with_regions, monkeypatch
):
    """Recomputing per request means the markers query on every manifest fetch, and the manifest
    is fetched on every map open and every theme toggle. Ten minutes of reuse is asserted against
    a clock the test moves — a sleep would make this case slow AND flaky.
    """
    now = [1_000.0]
    monkeypatch.setattr(map_coverage, "_monotonic", lambda: now[0])
    owner = await _member(adb, "coverage-cached")
    memo = await _memo(adb, owner)
    await _located(adb, memo, uid="gap-1", latitude=-10.0, longitude=-10.0)

    assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, DETAIL) == 1

    await _located(adb, memo, uid="gap-2", latitude=-11.0, longitude=-11.0)
    # Nine minutes and 59 seconds, spelled out rather than read off the constant: a test that
    # advanced by the window the code declares would agree with any window the code declared.
    now[0] += 599
    assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, DETAIL) == 1

    now[0] += 2  # past ten minutes
    # Positive control for the reuse above: the second pin WAS there to be counted all along.
    assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, DETAIL) == 2


async def test_a_pin_inside_the_regions_is_not_a_gap(adb, tiles_with_regions):
    owner = await _member(adb, "coverage-covered")
    memo = await _memo(adb, owner)
    await _located(adb, memo, uid="covered-1", latitude=47.5, longitude=8.5)

    assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, DETAIL) == 0


async def test_a_photo_the_feed_hides_is_not_counted_as_a_gap(adb, tiles_with_regions):
    """The markers query decides who sees what, and this reuses it rather than restating it: a
    second rule would drift, and a drift here counts another member's private photo."""
    from app.models import Visibility

    viewer = await _member(adb, "coverage-viewer")
    stranger = await _member(adb, "coverage-stranger")
    hidden = await _memo(adb, stranger, visibility=Visibility.PRIVATE)
    await _located(adb, hidden, uid="private-gap", latitude=-10.0, longitude=-10.0)

    assert await map_coverage.uncovered_for(adb, viewer, tiles_with_regions, DETAIL) == 0
    # Positive control: the same pin on the viewer's own memo IS counted.
    mine = await _memo(adb, viewer)
    await _located(adb, mine, uid="my-gap", latitude=-10.0, longitude=-10.0)
    map_coverage._CACHE.clear()
    assert await map_coverage.uncovered_for(adb, viewer, tiles_with_regions, DETAIL) == 1


async def test_two_callers_in_a_row_are_each_answered_about_their_own_pins(adb, tiles_with_regions):
    """One process-wide cache, and the answer is the CALLER's: what is visible decides the count.

    Asked one after the other with nothing cleared in between — the way two members hitting the
    manifest actually arrive — each must get their own number, and the first must still get theirs
    afterwards. A cache keyed by the archive alone would hand whoever asked second the other
    member's count, which is both wrong and a disclosure about photos they cannot see.
    """
    from app.models import Visibility

    first = await _member(adb, "coverage-pair-first")
    second = await _member(adb, "coverage-pair-second")
    mine = await _memo(adb, first, visibility=Visibility.PRIVATE)
    await _located(adb, mine, uid="pair-a1", latitude=-10.0, longitude=-10.0)
    theirs = await _memo(adb, second, visibility=Visibility.PRIVATE)
    await _located(adb, theirs, uid="pair-b1", latitude=-11.0, longitude=-11.0)
    await _located(adb, theirs, uid="pair-b2", latitude=-12.0, longitude=-12.0)

    assert await map_coverage.uncovered_for(adb, first, tiles_with_regions, DETAIL) == 1
    assert await map_coverage.uncovered_for(adb, second, tiles_with_regions, DETAIL) == 2
    # And the first one's own answer survives the second one asking.
    assert await map_coverage.uncovered_for(adb, first, tiles_with_regions, DETAIL) == 1


# --- what the gap may be logged as -------------------------------------------------------------


async def test_a_coverage_gap_is_logged_as_a_count_and_never_as_a_place(adb, tiles_with_regions):
    """The operator needs to learn that a re-extract is due; nothing about that need requires a
    coordinate, and a log line is the one surface that fans out to files and shippers."""
    owner = await _member(adb, "coverage-logged")
    memo = await _memo(adb, owner)
    await _located(adb, memo, uid="logged-gap", latitude=-10.25, longitude=-11.75)

    with capture_logs() as logs:
        assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, DETAIL) == 1

    [gap] = [entry for entry in logs if entry["event"] == "map.coverage_gap"]
    assert gap["count"] == 1
    assert gap["detail"] == DETAIL
    assert "latitude" not in gap and "longitude" not in gap
    rendered = json.dumps(logs)
    for spelling in ("latitude", "longitude", "-10.25", "-11.75", "10.25", "11.75"):
        assert spelling not in rendered, f"a log event spells out {spelling}: {rendered[:400]}"
    # Positive control for the search: it DOES find a coordinate in a line that carries one.
    probe = json.dumps([{"event": "probe", "latitude": -10.25}])
    assert all(s in probe for s in ("latitude", "-10.25"))


async def test_full_coverage_says_nothing_at_all(adb, tiles_with_regions):
    owner = await _member(adb, "coverage-quiet")
    memo = await _memo(adb, owner)
    await _located(adb, memo, uid="quiet-1", latitude=47.5, longitude=8.5)

    with capture_logs() as logs:
        assert await map_coverage.uncovered_for(adb, owner, tiles_with_regions, DETAIL) == 0

    assert [entry for entry in logs if entry["event"] == "map.coverage_gap"] == []
