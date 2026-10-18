"""The resumable camera backfill: what it selects, what it counts, what it writes (MEMNG-865, F2).

The run is a production data mutation, so the cases are the contract the orchestrator's prod run
leans on: the dry run writes nothing (every column of every row compared before and after); the
candidate set is the ledger predicate applied ONCE; `--uid` is the one-victim run; a visited row is
never revisited by a plain run; the six counters partition the candidates; a late write never
overwrites a row another writer already settled; and the UPDATE names only the camera columns.

Seeded with one file per outcome class, plus a file whose only camera field is invalid.
"""

import re
from pathlib import Path

import pytest
from app.scripts import backfill_camera as bf
from app.services import camera
from sqlalchemy import create_engine, event, text

from tests import camera_fixtures as fx
from tests.conftest import SYNC_URL


def _seed(rows: list[tuple[str, str, str | None, str | None]]) -> None:
    """One attachment per (uid, mime, storage_path, camera_source), owned by a throwaway member.

    A non-ready status and a NULL path are seeded on purpose for some rows: the population is
    every attachment row, with no status or path filter (F2).
    """
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner = c.execute(
            text(
                "INSERT INTO users (subject, display_name, role, created_at)"
                " VALUES ('camera-owner', 'camera-owner', 'user', now()) RETURNING id"
            )
        ).scalar_one()
        for uid, mime, path, source in rows:
            c.execute(
                text(
                    "INSERT INTO attachments (uid, owner_id, filename, mime, size, status,"
                    " storage_path, camera_source, latitude, longitude, location_source,"
                    " capture_source, created_at)"
                    " VALUES (:u, :o, :f, :m, 1, 'ready', :p, :s, 1.5, 2.5, 'exif', 'exif', now())"
                ),
                {"u": uid, "o": owner, "f": uid, "m": mime, "p": path, "s": source},
            )
        c.execute(
            text(
                "UPDATE attachments SET status = 'processing'"
                " WHERE uid IN ('h-pathless', 'c-unreadable')"
            )
        )
    eng.dispose()


def _rows() -> list[tuple]:
    """Every column of every attachment row — the oracle for "wrote nothing"."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            return [tuple(r) for r in c.execute(text("SELECT * FROM attachments ORDER BY id"))]
    finally:
        eng.dispose()


def _ledger() -> dict[str, tuple]:
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            return {
                r[0]: tuple(r[1:])
                for r in c.execute(
                    text(
                        "SELECT uid, camera_source, camera_make, iso, f_number,"
                        " latitude, longitude, location_source, capture_source FROM attachments"
                    )
                )
            }
    finally:
        eng.dispose()


@pytest.fixture
def library(db_engine, tmp_path):
    """The seeded set: one file per class, the invalid-only file, a path-less image, a video."""
    files = {
        "a-exif": fx.full_jpeg(tmp_path / "full.jpg"),
        "b-none": fx.bare_jpeg(tmp_path / "bare.jpg"),
        "c-unreadable": fx.corrupt(tmp_path / "bad.jpg"),
        "d-invalid": fx.invalid_only_jpeg(tmp_path / "inv.jpg"),
        "e-missing": tmp_path / "gone.jpg",
        "f-video": fx.tiny_mp4(tmp_path / "clip.mp4"),
        "g-heic": fx.make_model_heic(tmp_path / "p.heic"),
        "k-truncated": fx.truncated_ifd_jpeg(tmp_path / "trunc.jpg"),
    }
    rows: list[tuple[str, str, str | None, str | None]] = [
        ("a-exif", "image/jpeg", str(files["a-exif"]), None),
        ("b-none", "image/jpeg", str(files["b-none"]), None),
        ("c-unreadable", "image/jpeg", str(files["c-unreadable"]), None),
        ("d-invalid", "image/jpeg", str(files["d-invalid"]), None),
        ("e-missing", "image/jpeg", str(files["e-missing"]), None),
        ("f-video", "video/mp4", str(files["f-video"]), None),
        ("g-heic", "image/heic", str(files["g-heic"]), None),
        ("h-pathless", "image/png", None, None),
        ("i-visited", "image/jpeg", str(files["a-exif"]), "none"),
        ("j-was-unreadable", "image/jpeg", str(files["a-exif"]), "unreadable"),
        # Pillow only WARNS on this one (an IFD that declares an entry it does not hold)
        ("k-truncated", "image/jpeg", str(files["k-truncated"]), None),
    ]
    _seed(rows)
    yield files
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM users WHERE subject = 'camera-owner'"))
    eng.dispose()


@pytest.fixture
def declared_empty(db_engine, tmp_path):
    """Files that DECLARE EXIF and hold none, beside one that holds a complete empty IFD0."""
    files = {
        "m-prefix-only": fx.jpeg_app1(tmp_path / "prefix.jpg", fx.EXIF_PREFIX),
        "n-webp-empty": fx.webp_exif_chunk(tmp_path / "empty.webp", b""),
        "o-heif-empty": fx.heif_exif_extent(tmp_path / "empty.heic", b""),
        "p-empty-ifd0": fx.jpeg_app1(tmp_path / "ifd0.jpg", fx.EXIF_PREFIX + fx.EMPTY_CLASSIC),
        "q-png-oversized": fx.png_exif_chunk(
            tmp_path / "big.png", fx.oversized_exif(), after_idat=True
        ),
        "r-vp8x-flag-only": fx.webp_vp8x_without_exif_chunk(tmp_path / "flag.webp", flag=True),
        "s-webp-appended": fx.webp_with_exif_bytes_appended(tmp_path / "appended.webp"),
    }
    mimes = {
        "m": "image/jpeg",
        "n": "image/webp",
        "o": "image/heic",
        "p": "image/jpeg",
        "q": "image/png",
        "r": "image/webp",
        "s": "image/webp",
    }
    _seed([(uid, mimes[uid[0]], str(path), None) for uid, path in files.items()])
    yield files
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM users WHERE subject = 'camera-owner'"))
    eng.dispose()


async def test_a_declared_but_empty_exif_is_ledgered_unreadable_and_retried(declared_empty):
    stats = await bf.run_backfill(await bf.load_candidates(), execute=True)
    assert (stats.candidates, stats.unreadable, stats.none, stats.failed) == (7, 5, 2, 0)
    ledger = {uid: row[0] for uid, row in _ledger().items()}
    assert ledger == {
        "m-prefix-only": "unreadable",
        "n-webp-empty": "unreadable",
        "o-heif-empty": "unreadable",
        "p-empty-ifd0": "none",
        "q-png-oversized": "unreadable",
        "r-vp8x-flag-only": "unreadable",
        "s-webp-appended": "none",
    }
    retried = await bf.load_candidates(retry_unreadable=True)
    assert sorted(c.uid for c in retried) == [
        "m-prefix-only",
        "n-webp-empty",
        "o-heif-empty",
        "q-png-oversized",
        "r-vp8x-flag-only",
    ]


class StubReader:
    """The real reader, counted — so a test can prove a row was never read."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, path, mime):
        self.paths.append(str(path))
        return camera.read_camera(path, mime)


def _counts(stats: bf.BackfillStats) -> dict[str, int]:
    return {
        "candidates": stats.candidates,
        "exif": stats.exif,
        "none": stats.none,
        "unreadable": stats.unreadable,
        "not-image": stats.not_image,
        "unvisited": stats.unvisited,
        "failed": stats.failed,
    }


EXPECTED_FIRST_RUN = {
    "candidates": 9,
    "exif": 2,  # a-exif, g-heic
    "none": 2,  # b-none, d-invalid
    "unreadable": 4,  # c-unreadable, e-missing, h-pathless, k-truncated
    "not-image": 1,  # f-video
    "unvisited": 0,
    "failed": 0,
}


# --- the statement -------------------------------------------------------------------------


def _sql(**kwargs) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        bf.candidate_statement(**kwargs).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_the_statement_copies_none_of_the_location_filters():
    sql = _sql()
    assert "camera_source IS NULL" in sql
    for absent in ("status", "storage_path IS NOT NULL", "LIKE"):
        assert absent not in sql.split("WHERE", 1)[1]


def test_retry_unreadable_selects_only_the_unreadable_rows():
    where = _sql(retry_unreadable=True).split("WHERE", 1)[1]
    assert "camera_source = 'unreadable'" in where
    assert "IS NULL" not in where


# --- dry run --------------------------------------------------------------------------------


async def test_a_dry_run_counts_classes_summing_to_the_candidates_and_writes_nothing(library):
    before = _rows()
    reader = StubReader()
    stats = await bf.run_backfill(await bf.load_candidates(), execute=False, reader=reader)
    assert _counts(stats) == EXPECTED_FIRST_RUN
    assert stats.partition_holds()
    assert _rows() == before
    line = stats.format(execute=False).splitlines()[0]
    assert line == (
        "DRY RUN: candidates=9 exif=2 none=2 unreadable=4 not-image=1 unvisited=0 failed=0"
    )
    # the two visited rows were not read, nor were the video or the path-less row
    assert sorted(Path(p).name for p in reader.paths) == [
        "bad.jpg",
        "bare.jpg",
        "full.jpg",
        "gone.jpg",
        "inv.jpg",
        "p.heic",
        "trunc.jpg",
    ]


async def test_a_dry_run_issues_no_update(library):
    from app.database import engine

    seen: list[str] = []

    def _record(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        await bf.run_backfill(await bf.load_candidates(), execute=False)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert seen  # the listener saw the SELECT, so an UPDATE would have been seen too
    assert not [s for s in seen if re.match(r"\s*UPDATE", s, re.I)]


# --- execute --------------------------------------------------------------------------------


async def test_execute_uid_writes_exactly_one_row_including_the_ledger(library):
    before = {r[1]: r for r in _rows()}  # uid is column 1
    stats = await bf.run_backfill(await bf.load_candidates(uid="a-exif"), execute=True)
    assert stats.candidates == 1 and stats.exif == 1 and stats.failed == 0
    after = {r[1]: r for r in _rows()}
    changed = [uid for uid in before if before[uid] != after[uid]]
    assert changed == ["a-exif"]
    assert _ledger()["a-exif"][:2] == ("exif", "ACME")


async def test_execute_uid_of_an_already_visited_row_selects_nothing(library):
    assert await bf.load_candidates(uid="i-visited") == []


async def test_a_full_execute_writes_every_candidate_and_only_the_camera_columns(library):
    stats = await bf.run_backfill(await bf.load_candidates(), execute=True)
    assert _counts(stats) == EXPECTED_FIRST_RUN
    ledger = _ledger()
    assert {uid: row[0] for uid, row in ledger.items()} == {
        "a-exif": "exif",
        "b-none": "none",
        "c-unreadable": "unreadable",
        "d-invalid": "none",
        "e-missing": "unreadable",
        "f-video": "not-image",
        "g-heic": "exif",
        "h-pathless": "unreadable",
        "i-visited": "none",
        "j-was-unreadable": "unreadable",
        "k-truncated": "unreadable",
    }
    assert ledger["a-exif"][1:4] == ("ACME", 400, pytest.approx(2.8))
    assert ledger["d-invalid"][2] is None  # the invalid ISO never reached the column
    # capture and location columns are exactly as seeded on every row
    assert {row[4:] for row in ledger.values()} == {(1.5, 2.5, "exif", "exif")}


async def test_the_update_names_only_the_camera_columns(library):
    from app.database import engine

    seen: list[str] = []

    def _record(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("UPDATE"):
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        await bf.run_backfill(await bf.load_candidates(uid="a-exif"), execute=True)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert len(seen) == 1
    set_clause = seen[0].split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assigned = sorted(part.split("=")[0].strip() for part in set_clause.split(","))
    assert assigned == sorted(
        [
            "camera_make",
            "camera_model",
            "lens_model",
            "f_number",
            "exposure_time_s",
            "focal_length_mm",
            "exposure_bias_ev",
            "iso",
            "camera_source",
        ]
    )


async def test_a_second_plain_run_visits_nothing(library):
    await bf.run_backfill(await bf.load_candidates(), execute=True)
    reader = StubReader()
    candidates = await bf.load_candidates()
    stats = await bf.run_backfill(candidates, execute=True, reader=reader)
    assert candidates == []
    assert reader.paths == []
    assert stats.candidates == 0 and stats.partition_holds()


async def test_retry_unreadable_revisits_only_the_unreadable_rows(library):
    await bf.run_backfill(await bf.load_candidates(), execute=True)
    reader = StubReader()
    candidates = await bf.load_candidates(retry_unreadable=True)
    assert sorted(c.uid for c in candidates) == [
        "c-unreadable",
        "e-missing",
        "h-pathless",
        "j-was-unreadable",
        "k-truncated",
    ]
    stats = await bf.run_backfill(candidates, execute=True, reader=reader)
    # h-pathless has no path to read; the other three are read again
    assert sorted(Path(p).name for p in reader.paths) == [
        "bad.jpg",
        "full.jpg",
        "gone.jpg",
        "trunc.jpg",
    ]
    assert stats.exif == 1  # j-was-unreadable now reads
    assert _ledger()["j-was-unreadable"][0] == "exif"
    assert _ledger()["b-none"][0] == "none"


async def test_a_late_write_never_overwrites_a_row_another_writer_settled(library):
    # Candidates are materialized ONCE; a row settled by someone else between that SELECT and
    # this run's UPDATE keeps the other writer's answer, and the miss is counted, not hidden.
    candidates = await bf.load_candidates(uid="a-exif")
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("UPDATE attachments SET camera_source='none' WHERE uid='a-exif'"))
    eng.dispose()
    stats = await bf.run_backfill(candidates, execute=True)
    assert stats.exif == 1
    assert stats.failed == 1
    assert stats.partition_holds()
    assert _ledger()["a-exif"][:2] == ("none", None)


async def test_a_failed_write_keeps_its_class_and_counts_failed_once(library, monkeypatch):
    async def _no_write(candidate, info, source):
        return False

    monkeypatch.setattr(bf, "_store", _no_write)
    stats = await bf.run_backfill(await bf.load_candidates(), execute=True)
    assert _counts(stats) == {**EXPECTED_FIRST_RUN, "failed": 9}
    assert stats.partition_holds()


async def test_a_stop_reports_the_rest_as_unvisited_never_as_unreadable(library, monkeypatch):
    monkeypatch.setattr(bf, "MAX_CONSECUTIVE_FAILURES", 2)
    candidates = await bf.load_candidates()
    # b-none, c-unreadable, e-missing in id order after a-exif: two failures in a row stop it
    ordered = [c for c in candidates if c.uid in ("a-exif", "c-unreadable", "e-missing", "b-none")]
    ordered = [ordered[0], ordered[2], ordered[3], ordered[1]]
    stats = await bf.run_backfill(ordered, execute=True)
    assert stats.stopped_after_failures
    assert (stats.exif, stats.unreadable, stats.none, stats.unvisited) == (1, 2, 0, 1)
    assert stats.partition_holds()
    assert bf.exit_code(stats) == 1
    assert _ledger()["b-none"][0] is None


async def test_a_stalled_read_is_cut_off_as_unreadable(library, monkeypatch):
    import time

    monkeypatch.setattr(bf, "READ_TIMEOUT_S", 0.05)

    def _stall(path, mime):
        time.sleep(0.5)
        return camera.CameraInfo(make="late")

    stats = await bf.run_backfill(
        await bf.load_candidates(uid="a-exif"), execute=True, reader=_stall
    )
    assert stats.unreadable == 1
    assert _ledger()["a-exif"][:2] == ("unreadable", None)


async def test_limit_applies_after_the_ledger_predicate(library):
    candidates = await bf.load_candidates(limit=3)
    assert [c.uid for c in candidates] == ["a-exif", "b-none", "c-unreadable"]


def test_the_partition_assertion_refuses_a_broken_count():
    stats = bf.BackfillStats(candidates=3, exif=1, none=1)
    assert not stats.partition_holds()
    assert bf.exit_code(stats) == 1
    stats = bf.BackfillStats(candidates=2, exif=1, none=1, failed=3)
    assert not stats.partition_holds()
    healthy = bf.BackfillStats(candidates=2, exif=1, unvisited=1)
    assert healthy.partition_holds()
    assert bf.exit_code(healthy) == 0
    assert "partition=ok" in healthy.format(execute=True)


def test_a_run_whose_write_did_not_land_exits_nonzero_and_apart_from_a_broken_count():
    # The one-victim run: one row read, its single write superseded. The counts hold, so only
    # `failed` can say the run did not do what it was for.
    victim = bf.BackfillStats(candidates=1, exif=1, failed=1)
    assert victim.partition_holds()
    assert bf.exit_code(victim) == bf.EXIT_WRITES_FAILED != 0
    assert bf.EXIT_WRITES_FAILED not in (bf.EXIT_OK, bf.EXIT_BROKEN, 2)  # 2 = argparse usage
    broken = bf.BackfillStats(candidates=2, exif=1, failed=1)
    assert bf.exit_code(broken) == bf.EXIT_BROKEN
    stopped = bf.BackfillStats(candidates=1, unreadable=1, failed=1, stopped_after_failures=True)
    assert bf.exit_code(stopped) == bf.EXIT_BROKEN


async def test_a_one_victim_run_whose_write_is_superseded_exits_nonzero(library, monkeypatch):
    # `main` is `exit_code(asyncio.run(_amain(...)))`; asyncio.run cannot nest in this loop, so
    # the run itself is awaited and its stats judged by the same function main returns.
    async def _no_write(candidate, info, source):
        return False

    monkeypatch.setattr(bf, "_store", _no_write)
    stats = await bf._amain(
        execute=True, retry_unreadable=False, uid="a-exif", limit=None, sleep=0.0
    )
    assert (stats.candidates, stats.exif, stats.failed) == (1, 1, 1)
    assert bf.exit_code(stats) == bf.EXIT_WRITES_FAILED
