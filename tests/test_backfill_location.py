"""The one-shot location backfill: what it selects, what it writes, and when it stops.

Two of these cases exist because the run is rehearsed against PRODUCTION before the migration is
applied there: the dry run's statement may not name a column that does not exist yet, and the dry
run may not write. The rest are the resume contract — a row read once is never read again, so a
killed run costs at most one file — and the stop that keeps a dead NFS mount from turning the whole
library into `'unreadable'` in a few seconds.
"""

import json
import re
from pathlib import Path

import pytest
from app.scripts import backfill_location as bf
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL
from tests.test_location_privacy import LEAKED_SPELLINGS

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "locations"
WITH_GPS = FIXTURES / "jpeg-north-east.jpg"
NO_GPS = FIXTURES / "jpeg-no-gps.jpg"
MISSING = FIXTURES / "does-not-exist.jpg"


def _seed(rows: list[tuple[str, Path, str | None]]) -> None:
    """One ready attachment per (uid, path, location_source), owned by a throwaway member."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner = c.execute(
            text(
                "INSERT INTO users (subject, display_name, role, created_at)"
                " VALUES ('backfill-owner', 'backfill-owner', 'user', now()) RETURNING id"
            )
        ).scalar_one()
        memo = c.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at,"
                " updated_at) VALUES ('backfill-memo', :o, '', 'PRIVATE', false, now(), now())"
                " RETURNING id"
            ),
            {"o": owner},
        ).scalar_one()
        for uid, path, source in rows:
            c.execute(
                text(
                    "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size,"
                    " status, storage_path, location_source, created_at)"
                    " VALUES (:u, :o, :m, :f, 'image/jpeg', 1, 'ready', :p, :s, now())"
                ),
                {"u": uid, "o": owner, "m": memo, "f": f"{uid}.jpg", "p": str(path), "s": source},
            )
    eng.dispose()


def _read_back() -> dict[str, tuple[float | None, float | None, str | None]]:
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            return {
                uid: (lat, lon, src)
                for uid, lat, lon, src in c.execute(
                    text("SELECT uid, latitude, longitude, location_source FROM attachments")
                )
            }
    finally:
        eng.dispose()


@pytest.fixture
def seeded(db_engine):
    yield _seed
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM memos"))
        c.execute(text("DELETE FROM users WHERE subject = 'backfill-owner'"))
    eng.dispose()


def _sql(**kwargs) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        bf.candidate_statement(**kwargs).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# --- the statement ------------------------------------------------------------------------------


def test_the_dry_run_statement_names_no_location_column():
    # The release train rehearses the dry run against the PRE-migration production database, where
    # these three columns do not exist yet: naming one would make the rehearsal impossible.
    sql = _sql(execute=False, retry_unreadable=False)
    for column in ("latitude", "longitude", "location_source"):
        assert column not in sql


def test_the_execute_statement_skips_rows_already_read():
    sql = _sql(execute=True, retry_unreadable=False)
    assert "location_source IS NULL" in sql
    assert "unreadable" not in sql


def test_retry_unreadable_widens_the_filter_to_the_failed_rows_only():
    sql = _sql(execute=True, retry_unreadable=True)
    assert "location_source IS NULL" in sql
    assert "'unreadable'" in sql
    assert "'none'" not in sql  # a file that HAS no location is never re-read


# --- what a run does ----------------------------------------------------------------------------


async def test_a_dry_run_reads_the_files_and_writes_nothing(seeded):
    seeded([("dry1", WITH_GPS, None)])
    stats = await bf.run_backfill(await bf.load_candidates(execute=False), execute=False)

    assert (stats.processed, stats.located) == (1, 1)
    assert stats.bytes_read > 0
    assert _read_back()["dry1"] == (None, None, None)


async def test_execute_with_limit_one_writes_exactly_one_row(seeded):
    seeded([("one", WITH_GPS, None), ("two", WITH_GPS, None)])
    stats = await bf.run_backfill(await bf.load_candidates(execute=True, limit=1), execute=True)

    assert stats.processed == 1
    written = [uid for uid, (lat, _, _) in _read_back().items() if lat is not None]
    assert len(written) == 1
    assert _read_back()[written[0]] == (12.5, 45.25, "exif")


async def test_a_second_run_does_not_read_a_row_again(seeded):
    seeded([("resume", WITH_GPS, None)])
    await bf.run_backfill(await bf.load_candidates(execute=True), execute=True)

    again = await bf.load_candidates(execute=True)
    assert [c.uid for c in again] == []


async def test_a_file_without_a_location_is_recorded_and_never_re_read(seeded):
    seeded([("empty", NO_GPS, None)])
    stats = await bf.run_backfill(await bf.load_candidates(execute=True), execute=True)

    assert (stats.located, stats.sources["none"]) == (0, 1)
    assert _read_back()["empty"] == (None, None, "none")
    assert [c.uid for c in await bf.load_candidates(execute=True)] == []
    # ...and --retry-unreadable does not resurrect it either: it was read, and it said nothing.
    assert [c.uid for c in await bf.load_candidates(execute=True, retry_unreadable=True)] == []


async def test_an_unreadable_original_is_marked_and_retried_only_on_demand(seeded):
    seeded([("gone", MISSING, None), ("fine", WITH_GPS, "exif")])
    stats = await bf.run_backfill(await bf.load_candidates(execute=True), execute=True)

    assert (stats.failed, stats.sources["unreadable"]) == (1, 1)
    assert _read_back()["gone"] == (None, None, "unreadable")
    assert [c.uid for c in await bf.load_candidates(execute=True, retry_unreadable=True)] == [
        "gone"
    ]


async def test_the_run_stops_after_twenty_consecutive_read_failures(seeded):
    # A dead NFS mount fails every read: without this stop the run would write 'unreadable' over
    # the whole library in seconds, and the retry flag would be the only way back.
    seeded([(f"nfs{i}", MISSING, None) for i in range(bf.MAX_CONSECUTIVE_FAILURES + 5)])
    candidates = await bf.load_candidates(execute=True)
    stats = await bf.run_backfill(candidates, execute=True)

    assert stats.failed == bf.MAX_CONSECUTIVE_FAILURES
    assert stats.stopped_after_failures is True
    assert bf.exit_code(stats) == 1


async def test_a_success_between_failures_resets_the_counter(seeded):
    rows = [(f"nfs{i}", MISSING, None) for i in range(bf.MAX_CONSECUTIVE_FAILURES - 1)]
    rows.append(("good", WITH_GPS, None))
    rows += [(f"late{i}", MISSING, None) for i in range(3)]
    seeded(rows)
    stats = await bf.run_backfill(await bf.load_candidates(execute=True), execute=True)

    assert stats.stopped_after_failures is False
    assert stats.located == 1
    assert bf.exit_code(stats) == 0


async def test_a_hot_host_defers_instead_of_reading(seeded, monkeypatch):
    seeded([("hot", WITH_GPS, None)])
    answers = iter([True, True, False])
    monkeypatch.setattr(bf, "too_hot", lambda: next(answers))
    monkeypatch.setattr(bf, "THERMAL_SLEEP_S", 0)

    stats = await bf.run_backfill(await bf.load_candidates(execute=True), execute=True)

    assert stats.deferred == 2
    assert stats.located == 1


# --- what it prints -----------------------------------------------------------------------------


def test_the_summary_carries_counts_and_no_coordinate():
    stats = bf.BackfillStats(candidates=3, processed=3, located=2, bytes_read=4096)
    stats.sources.update(["exif", "quicktime", "none"])
    stats.cells.update([(12, 45), (12, 45), (-34, -26)])

    text_out = stats.format(execute=True)
    assert "12.5" not in text_out and "45.25" not in text_out
    # A cell identity IS a coordinate, rounded — so the ranking publishes ranks and counts only.
    ranking = text_out.split("top cells")[1].strip().splitlines()
    assert [re.fullmatch(r"\s*#\d+ +\d+ pins", line) is not None for line in ranking] == [
        True,
        True,
    ]
    assert "exif=1" in text_out and "bytes_read=4096" in text_out


def test_the_regions_file_is_one_buffered_cell_per_pin_cluster(tmp_path):
    stats = bf.BackfillStats()
    stats.cells.update([(12, 45), (12, 45)])
    out = tmp_path / "regions.geojson"

    bf.write_regions(stats, out)

    geo = json.loads(out.read_text())
    [feature] = geo["features"]
    assert feature["geometry"]["type"] == "MultiPolygon"
    [[ring]] = feature["geometry"]["coordinates"]
    lons = [lon for lon, _ in ring]
    lats = [lat for _, lat in ring]
    # the 1° cell at (45E, 12N), buffered by half a degree on every side
    assert (min(lons), max(lons)) == (44.5, 46.5)
    assert (min(lats), max(lats)) == (11.5, 13.5)


async def test_no_line_a_run_emits_carries_a_coordinate(seeded):
    # Logs leave this machine (they are shipped, tailed and pasted into tickets); the database does
    # not. So the value is written to the row and to nothing else — on the success path AND on the
    # failure path, which is why both rows are seeded here.
    seeded([("logged", WITH_GPS, None), ("alsogone", MISSING, None)])

    with capture_logs() as logs:
        stats = await bf.run_backfill(await bf.load_candidates(execute=True), execute=True)

    assert (stats.located, stats.failed) == (1, 1)
    assert len(logs) >= 2  # positive control: the capture really did see this run's lines
    emitted = json.dumps(logs) + stats.format(execute=True)
    assert LEAKED_SPELLINGS  # positive control: there IS something to look for
    for spelling in LEAKED_SPELLINGS:
        assert spelling not in emitted
    # ...and the row it just wrote does hold it, or the assertion above proves nothing.
    assert _read_back()["logged"][:2] == (12.5, 45.25)
