"""A guest batch is filed as one memo per capture day (MEMNG-1266).

Guests send many pictures at once, often from several days. One door batch used to become ONE memo
dated by its earliest picture, so ten pictures from 1-4 November read as one memo of 1 November.
Now every file lands on a day — the camera's wall clock as written (`capture_civil`), else its
instant in the trip's zone, else its client mtime in that zone, else the day home recorded the
batch — and each day becomes its own guest memo, with the batch's caption, dated by its OWN files
(MEMNG-1262). A later part of the same batch goes into the memo of ITS day.

The filing cases run the REAL pull: door upload, home tick, the guest memo boundary, the ledger.
"""

import asyncio
import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.database import async_session
from app.models import IngestBlob
from app.services import trip_ingest
from PIL import Image
from sqlalchemy import select

from tests import test_trip_ingest_puller as tp
from tests.camera_fixtures import exif_bytes
from tests.ingest_support import JPEG, complete, upload

pluto_schema = tp.pluto_schema
world = tp.world

DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011
BOGOTA = "America/Bogota"  # UTC-5 all year: no DST edge muddies the day boundary
NOV = [datetime(2031, 11, d, 9, 0, tzinfo=UTC) for d in range(1, 5)]


def _jpeg(colour: int, *, civil: datetime | None = None, instant: datetime | None = None) -> bytes:
    """An 8x8 JPEG. `civil`: EXIF DateTimeOriginal with no offset, read as a wall clock only.
    `instant`: the same tag with +02:00, read as a point in time."""
    sub: dict[int, object] = {}
    if civil is not None:
        sub[DATETIME_ORIGINAL] = civil.strftime("%Y:%m:%d %H:%M:%S")
    if instant is not None:
        sub[DATETIME_ORIGINAL] = (instant + timedelta(hours=2)).strftime("%Y:%m:%d %H:%M:%S")
        sub[OFFSET_TIME_ORIGINAL] = "+02:00"
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (colour % 256, colour // 256, 77)).save(
        out, "JPEG", exif=exif_bytes({}, sub) if sub else b""
    )
    return out.getvalue()


def _ms(instant: datetime) -> int:
    return int(instant.timestamp() * 1000)


async def _send(w, token: str, files, caption: str | None = "Beach week") -> str:
    """files: (bytes, name, client mtime or None)."""
    uids = []
    for data, name, mtime in files:
        extra = {"last_modified": _ms(mtime)} if mtime is not None else {}
        uids.append(await upload(w.door, token, data, name, **extra))
    r = await complete(w.door, token, uids, caption)
    assert r.status_code in (200, 201), r.text
    return r.json()["batch_id"]


async def _batch(w, files, *, tz: str | None = None, caption: str | None = "Beach week") -> str:
    trip = await tp._trip(w)
    if tz is not None:
        tp._sql("UPDATE collections SET tz = :z WHERE id = :c", {"z": tz, "c": trip.collection_id})
    return await _send(w, await tp._guest(w, trip), files, caption)


def _memo_days() -> list[tuple[date, int, str | None]]:
    """Every memo: its created_at's UTC date, how many pictures it holds, its caption."""
    rows = tp._sql(
        "SELECT m.created_at, count(a.id), m.guest_caption FROM memos m "
        "LEFT JOIN attachments a ON a.memo_id = m.id GROUP BY m.id ORDER BY m.created_at"
    )
    return [(c.astimezone(UTC).date(), n, cap) for c, n, cap in rows]


def _ledger(batch: str) -> list[tuple[date, bool]]:
    return [
        (day, memo_id is not None)
        for day, memo_id in tp._sql(
            "SELECT bm.day, bm.memo_id FROM ingest_batch_memos bm "
            "JOIN ingest_batches b ON b.id = bm.batch_id WHERE b.uid = :u ORDER BY bm.day",
            {"u": batch},
        )
    ]


def _first_memo_of(batch: str) -> tuple[date, ...]:
    """The day of the memo `ingest_batches.memo_id` names, which stays the earliest day's."""
    return tuple(
        r[0]
        for r in tp._sql(
            "SELECT bm.day FROM ingest_batches b JOIN ingest_batch_memos bm "
            "ON bm.batch_id = b.id AND bm.memo_id = b.memo_id WHERE b.uid = :u",
            {"u": batch},
        )
    )


# --- the day rule: one case per input category ------------------------------------------------

UTC_ZONE = ZoneInfo("UTC")
ZONE = ZoneInfo(BOGOTA)
LATE = datetime(2031, 11, 2, 4, 30, tzinfo=UTC)  # 23:30 on 1 November in Bogota
RECORDED = datetime(2031, 11, 9, 12, 0, tzinfo=UTC)


def test_a_civil_capture_is_its_own_day_whatever_the_zone_says():
    day = trip_ingest.capture_day("2031-11-01T23:30:00", None, LATE, ZoneInfo("Asia/Tokyo"), LATE)
    assert day == date(2031, 11, 1)


def test_an_instant_lands_on_the_trip_zone_s_day_not_the_utc_one():
    assert trip_ingest.capture_day(None, LATE, None, ZONE, RECORDED) == date(2031, 11, 1)
    assert trip_ingest.capture_day(None, LATE, None, UTC_ZONE, RECORDED) == date(2031, 11, 2)


def test_with_no_capture_the_client_mtime_decides_in_the_trip_zone():
    assert trip_ingest.capture_day(None, None, LATE, ZONE, RECORDED) == date(2031, 11, 1)
    assert trip_ingest.capture_day(None, None, LATE, UTC_ZONE, RECORDED) == date(2031, 11, 2)


def test_an_instant_outranks_the_client_mtime():
    mtime = datetime(2031, 12, 25, 12, tzinfo=UTC)
    assert trip_ingest.capture_day(None, LATE, mtime, ZONE, RECORDED) == date(2031, 11, 1)


def test_with_nothing_stated_the_recorded_day_is_the_day():
    assert trip_ingest.capture_day(None, None, None, ZONE, RECORDED) == date(2031, 11, 9)


def test_a_civil_string_that_does_not_parse_falls_through_to_the_next_source():
    assert trip_ingest.capture_day("2031-11-01 junk", None, LATE, ZONE, RECORDED) == date(
        2031, 11, 1
    )
    assert trip_ingest.capture_day("", LATE, None, UTC_ZONE, RECORDED) == date(2031, 11, 2)


def test_a_moment_the_zone_cannot_hold_takes_its_utc_day():
    """`9999-12-31T23:59:59Z` is a valid last_modified (the puller files it) and, moved east of
    UTC, would be year 10000: the day rule must not be the step that refuses the batch."""
    end = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
    kiritimati = ZoneInfo("Pacific/Kiritimati")  # UTC+14
    assert trip_ingest.capture_day(None, None, end, kiritimati, RECORDED) == date(9999, 12, 31)


def test_a_naive_moment_is_read_as_utc_not_as_the_host_clock():
    naive = datetime(2031, 11, 2, 4, 30)
    assert trip_ingest.capture_day(None, None, naive, ZONE, RECORDED) == date(2031, 11, 1)


def test_the_trip_zone_falls_back_to_utc():
    assert trip_ingest.trip_zone(BOGOTA) == ZONE
    assert trip_ingest.trip_zone(None) == UTC_ZONE
    assert trip_ingest.trip_zone("Not/AZone") == UTC_ZONE


def test_a_print_files_on_the_day_of_its_screen():
    """A screen and its print are one picture (`finalize_pending` binds them inside one call), so
    they share a day even when their dates disagree: the screen's."""
    d1, d2, d3 = date(2031, 11, 1), date(2031, 11, 2), date(2031, 11, 3)
    days = trip_ingest.split_by_day(
        [
            ("u1", "IMG_1 - print.jpg", d3),
            ("u2", "IMG_1.jpg", d2),
            ("u3", "IMG_2.jpg", d1),
            ("u4", "IMG_3 - print.jpg", d3),
        ]
    )
    assert days == [(d1, ["u3"]), (d2, ["u1", "u2"]), (d3, ["u4"])]


def test_two_screens_of_one_name_keep_their_own_days_and_a_print_takes_the_earliest():
    """Two screens with one name are two pictures (two `IMG_0001.jpg`, Safari's `image.jpg`), so
    each keeps its own day. A print of that name is bound inside the finalize of ONE day, so it
    files on the earliest of its screens' days, where it finds one of them."""
    d1, d2, d3 = date(2031, 11, 1), date(2031, 11, 2), date(2031, 11, 3)
    days = trip_ingest.split_by_day(
        [
            ("u1", "image.jpg", d2),
            ("u2", "image - print.jpg", d3),
            ("u3", "image.jpg", d1),
        ]
    )
    assert days == [(d1, ["u2", "u3"]), (d2, ["u1"])]


def test_split_by_day_keeps_the_manifest_order_inside_a_day():
    d = date(2031, 11, 1)
    assert trip_ingest.split_by_day([("b", "b.jpg", d), ("a", "a.jpg", d)]) == [(d, ["b", "a"])]


# --- filing ------------------------------------------------------------------------------------


async def test_a_batch_over_four_days_is_four_memos_each_with_the_caption(world):
    """The owner's example: 3 + 3 + 3 + 1 pictures over 1-4 November, sent in a shuffled order."""
    files = []
    colour = 0
    for day, count in zip(NOV, (3, 3, 3, 1), strict=True):
        for i in range(count):
            shot = day + timedelta(hours=i)
            colour += 1
            files.append((_jpeg(colour, civil=shot), f"d{day.day}_{i}.jpg", shot))
    files = files[1::2] + files[::2]
    batch = await _batch(world, files)
    assert await tp._tick(world) == "ok"
    assert _memo_days() == [
        (date(2031, 11, 1), 3, "Beach week"),
        (date(2031, 11, 2), 3, "Beach week"),
        (date(2031, 11, 3), 3, "Beach week"),
        (date(2031, 11, 4), 1, "Beach week"),
    ]
    # Each memo is dated by its own earliest picture, not the batch's.
    assert [c.astimezone(UTC) for (c,) in tp._sql("SELECT created_at FROM memos ORDER BY 1")] == (
        NOV
    )
    # Every memo is a guest memo of this pass, exactly as a single one is.
    assert tp._sql(
        "SELECT count(*), count(DISTINCT pass_id), min(visibility), max(visibility) FROM memos"
    ) == [(4, 1, "PROTECTED", "PROTECTED")]
    assert {tag for (mid, *_) in tp._memos() for tag in tp._tags(mid)} == {tp.TAG}
    assert tp._sql("SELECT DISTINCT contributor_name FROM attachments") == [("Anna",)]
    assert _ledger(batch) == [(d.date(), True) for d in NOV]
    assert _first_memo_of(batch) == (date(2031, 11, 1),)
    assert tp._home_batch(batch) == [("acked", True, True, False)]


async def test_a_batch_of_one_day_is_one_memo(world):
    shots = [NOV[1] + timedelta(hours=h) for h in (5, 0, 9)]
    files = [(_jpeg(i, civil=s), f"p{i}.jpg", s) for i, s in enumerate(shots)]
    batch = await _batch(world, files)
    assert await tp._tick(world) == "ok"
    assert _memo_days() == [(date(2031, 11, 2), 3, "Beach week")]
    assert _ledger(batch) == [(date(2031, 11, 2), True)]
    [(memo_id,)] = tp._sql("SELECT memo_id FROM ingest_batches WHERE uid = :u", {"u": batch})
    assert tp._sql("SELECT id FROM memos") == [(memo_id,)]


@pytest.mark.parametrize(("tz", "memos"), [(BOGOTA, 2), (None, 1)], ids=["bogota", "no_zone"])
async def test_an_instant_only_capture_splits_on_the_trip_zone_s_midnight(world, tz, memos):
    """23:30 and 01:00 in Bogota are two days there and one day (2 November) in UTC."""
    first, second = LATE, LATE + timedelta(hours=1, minutes=30)
    files = [
        (_jpeg(1, instant=first), "late.jpg", None),
        (_jpeg(2, instant=second), "early.jpg", None),
    ]
    await _batch(world, files, tz=tz)
    assert await tp._tick(world) == "ok"
    rows = tp._sql("SELECT created_at FROM memos ORDER BY created_at")
    assert len(rows) == memos
    assert rows[0][0].astimezone(UTC) == first  # the earliest instant dates its memo
    if memos == 2:
        assert rows[1][0].astimezone(UTC) == second


async def test_pictures_with_no_capture_group_by_client_mtime_and_none_is_dropped(world):
    a = datetime(2031, 11, 1, 10, tzinfo=UTC)
    c = datetime(2031, 11, 3, 10, tzinfo=UTC)
    files = [
        (JPEG, "a.jpg", a),
        (JPEG + b"b", "b.jpg", a + timedelta(hours=3)),
        (JPEG + b"c", "c.jpg", c),
        (JPEG + b"d", "d.jpg", None),  # states nothing at all: the day home recorded the batch
    ]
    batch = await _batch(world, files, caption=None)
    assert await tp._tick(world) == "ok"
    [(recorded,)] = tp._sql("SELECT created_at FROM ingest_batches WHERE uid = :u", {"u": batch})
    today = recorded.astimezone(UTC).date()
    assert _memo_days() == [
        (today, 1, None),
        (date(2031, 11, 1), 2, None),
        (date(2031, 11, 3), 1, None),
    ]
    assert tp._sql("SELECT count(*) FROM ingest_blobs WHERE attachment_id IS NULL") == [(0,)]


async def test_two_batches_of_the_same_day_are_two_memos(world):
    """The day memo is the BATCH's: a second batch on the same day never joins the first's."""
    trip = await tp._trip(world)
    token = await tp._guest(world, trip)
    await _send(world, token, [(_jpeg(1, civil=NOV[0]), "a.jpg", NOV[0])])
    assert await tp._tick(world) == "ok"
    await _send(world, token, [(_jpeg(2, civil=NOV[0]), "b.jpg", NOV[0])])
    assert await tp._tick(world) == "ok"
    assert _memo_days() == [(date(2031, 11, 1), 1, "Beach week")] * 2


# --- later parts -------------------------------------------------------------------------------


async def _downloaded_in_parts(w, monkeypatch, files) -> tuple[str, int]:
    """A batch recorded and downloaded but not filed, one file per filing part."""
    monkeypatch.setattr(trip_ingest, "FILE_PART_FILES", 1)
    trip = await tp._trip(w)
    batch = await _send(w, await tp._guest(w, trip), files)
    real = trip_ingest.file_open
    monkeypatch.setattr(trip_ingest, "file_open", tp._no_filing)
    assert await tp._tick(w) == "ok"
    monkeypatch.setattr(trip_ingest, "file_open", real)
    [(batch_id,)] = tp._sql("SELECT id FROM ingest_batches WHERE uid = :u", {"u": batch})
    async with async_session() as db:
        rows = list(
            (await db.execute(select(IngestBlob).where(IngestBlob.batch_id == batch_id))).scalars()
        )
    await trip_ingest.download(w.link, batch, rows, deadline=float("inf"))
    return batch, batch_id


A = (_jpeg(1, civil=NOV[0]), "a.jpg", NOV[0])
B = (_jpeg(2, civil=NOV[0] + timedelta(hours=2)), "b.jpg", NOV[0] + timedelta(hours=2))
C = (_jpeg(3, civil=NOV[1]), "c.jpg", NOV[1])
# The ledger keeps a batch's files in its own order, not the manifest's: each case is written so
# that the order its parts are filed in does not matter.


async def test_a_later_part_of_the_same_day_appends_to_that_day_s_memo(world, monkeypatch):
    batch, batch_id = await _downloaded_in_parts(world, monkeypatch, [A, B])
    assert await trip_ingest._file_part(batch_id) is False
    [(first,)] = tp._sql("SELECT id FROM memos")
    assert await trip_ingest._file_part(batch_id) is True
    assert tp._sql("SELECT id FROM memos") == [(first,)]
    assert _memo_days() == [(date(2031, 11, 1), 2, "Beach week")]
    assert _ledger(batch) == [(date(2031, 11, 1), True)]


async def test_a_later_part_of_a_new_day_starts_that_day_s_memo(world, monkeypatch):
    batch, batch_id = await _downloaded_in_parts(world, monkeypatch, [A, C])
    assert await trip_ingest._file_part(batch_id) is False
    assert len(tp._memos()) == 1
    assert await trip_ingest._file_part(batch_id) is True
    assert _memo_days() == [
        (date(2031, 11, 1), 1, "Beach week"),
        (date(2031, 11, 2), 1, "Beach week"),
    ]
    assert _ledger(batch) == [(date(2031, 11, 1), True), (date(2031, 11, 2), True)]
    # Whichever day was filed first, the batch's one memo is the earliest day's.
    assert _first_memo_of(batch) == (date(2031, 11, 1),)


async def test_a_later_part_whose_day_memo_was_deleted_starts_a_fresh_one(world, monkeypatch):
    _batch_uid, batch_id = await _downloaded_in_parts(world, monkeypatch, [A, B])
    assert await trip_ingest._file_part(batch_id) is False
    [(first,)] = tp._sql("SELECT id FROM memos")
    tp._sql("DELETE FROM memos WHERE id = :m", {"m": first})  # the owner deleted it
    assert await trip_ingest._file_part(batch_id) is True
    [(fresh,)] = tp._sql("SELECT id FROM memos")
    assert fresh != first
    assert _memo_days() == [(date(2031, 11, 1), 1, "Beach week")]
    assert tp._sql(
        "SELECT day, memo_id FROM ingest_batch_memos WHERE batch_id = :b", {"b": batch_id}
    ) == [(date(2031, 11, 1), fresh)]
    assert tp._sql("SELECT memo_id FROM ingest_batches WHERE id = :b", {"b": batch_id}) == [
        (fresh,)
    ]


async def test_a_day_whose_memo_lives_never_gets_a_second_memo(world, monkeypatch):
    """The create is keyed by (batch, day): with the ledger row lost and the memo alive, the next
    part of that day is refused by the memo key, not filed as a second memo of the day."""
    _batch_uid, batch_id = await _downloaded_in_parts(world, monkeypatch, [A, B])
    assert await trip_ingest._file_part(batch_id) is False
    tp._sql("DELETE FROM ingest_batch_memos WHERE batch_id = :b", {"b": batch_id})
    tp._sql("UPDATE ingest_batches SET memo_id = NULL WHERE id = :b", {"b": batch_id})
    with pytest.raises(Exception, match="uq_memos_owner_idempotency_key"):
        await trip_ingest._file_part(batch_id)
    assert len(tp._memos()) == 1
    assert tp._sql("SELECT count(*) FROM ingest_blobs WHERE filed_at IS NULL") == [(1,)]


async def test_two_filings_racing_over_a_two_day_batch_make_one_memo_per_day(world, monkeypatch):
    _batch_uid, batch_id = await _downloaded_in_parts(world, monkeypatch, [A, B, C])
    results = await asyncio.gather(
        trip_ingest.file_batch(batch_id), trip_ingest.file_batch(batch_id), return_exceptions=True
    )
    assert sorted(results, key=repr) == [False, True], results
    assert _memo_days() == [
        (date(2031, 11, 1), 2, "Beach week"),
        (date(2031, 11, 2), 1, "Beach week"),
    ]
    assert tp._sql("SELECT count(*), count(DISTINCT attachment_id) FROM ingest_blobs") == [(3, 3)]


async def test_a_later_part_with_an_earlier_capture_re_dates_its_day_s_memo(world, monkeypatch):
    """A day memo is dated by the earliest capture of ALL its files (MEMNG-1262 per day), so a
    later part carrying an earlier picture of that day moves it. The memo keeps the one folder its
    first part named: a folder is named from `created_at` when a file is placed and is never
    renamed, so the later file joins the first one there, under the first part's HH:MM."""
    late, early = NOV[0] + timedelta(hours=6), NOV[0]
    files = [
        (_jpeg(1, civil=late), "late.jpg", late),
        (_jpeg(2, civil=early), "early.jpg", early),
    ]
    _batch_uid, batch_id = await _downloaded_in_parts(world, monkeypatch, files)
    real = trip_ingest._filing_parts
    monkeypatch.setattr(
        trip_ingest,
        "_filing_parts",
        lambda blobs: sorted(real(blobs), key=lambda part: part[0].filename != "late.jpg"),
    )
    assert await trip_ingest._file_part(batch_id) is False
    [(created,)] = tp._sql("SELECT created_at FROM memos")
    assert created.astimezone(UTC) == late
    assert await trip_ingest._file_part(batch_id) is True
    [(created,)] = tp._sql("SELECT created_at FROM memos")
    assert created.astimezone(UTC) == early
    folders = {p.rsplit("/", 1)[0] for (p,) in tp._sql("SELECT storage_path FROM attachments")}
    assert len(folders) == 1 and folders.pop().rsplit("/", 1)[1].startswith("2031-11-01_1500_")


# --- a camera clock with no offset dates its day memo (fold round) -----------------------------


def _folder_names() -> set[str]:
    return {p.rsplit("/", 2)[1] for (p,) in tp._sql("SELECT storage_path FROM attachments")}


async def test_civil_only_pictures_date_their_day_memos_by_the_camera_clock(world):
    """A camera with no offset (most non-phone cameras) and an export a week later: each day's
    memo is dated by the camera clock read in the trip's zone, folder included, not by the mtime."""
    first, second = datetime(2031, 11, 1, 9, 0), datetime(2031, 11, 2, 9, 30)
    week = timedelta(days=7)
    files = [
        (_jpeg(1, civil=first), "a.jpg", first.replace(tzinfo=UTC) + week),
        (_jpeg(2, civil=second), "b.jpg", second.replace(tzinfo=UTC) + week),
    ]
    await _batch(world, files, tz=BOGOTA)
    assert await tp._tick(world) == "ok"
    dated = [c.astimezone(UTC) for (c,) in tp._sql("SELECT created_at FROM memos ORDER BY 1")]
    assert dated == [
        first.replace(tzinfo=ZONE).astimezone(UTC),  # 09:00 in Bogota is 14:00 UTC
        second.replace(tzinfo=ZONE).astimezone(UTC),
    ]
    assert {n[:15] for n in _folder_names()} == {"2031-11-01_1400", "2031-11-02_1430"}


@pytest.mark.parametrize("civil_first", [True, False], ids=["civil_earlier", "instant_earlier"])
async def test_a_civil_capture_and_an_instant_on_one_day_the_earliest_dates_it(world, civil_first):
    civil = datetime(2031, 11, 1, 8, 0)  # 13:00 UTC in Bogota
    instant = datetime(2031, 11, 1, 15 if civil_first else 12, 0, tzinfo=UTC)
    files = [
        (_jpeg(1, instant=instant), "phone.jpg", None),
        (_jpeg(2, civil=civil), "camera.jpg", datetime(2031, 11, 20, tzinfo=UTC)),
    ]
    await _batch(world, files, tz=BOGOTA)
    assert await tp._tick(world) == "ok"
    [(created,)] = tp._sql("SELECT created_at FROM memos")
    expected = civil.replace(tzinfo=ZONE).astimezone(UTC) if civil_first else instant
    assert created.astimezone(UTC) == expected


# --- a day with no stated capture: its OWN files' client mtimes (rebase onto MEMNG-1262) -------


async def test_an_undated_day_memo_takes_its_own_day_s_client_mtimes(world):
    """Each day memo whose files state no capture is dated by the mtimes of THAT day's files: not
    by an earlier mtime of another day, which would date it outside its own day, and not by the
    filing time while its files carry an mtime. Only a day whose files said nothing at all takes
    the filing's own time."""
    first = datetime(2031, 11, 1, 10, tzinfo=UTC)
    third = datetime(2031, 11, 3, 8, tzinfo=UTC)
    files = [
        (JPEG, "a.jpg", first + timedelta(hours=3)),
        (JPEG + b"b", "b.jpg", first),
        (JPEG + b"c", "c.jpg", third),
        (JPEG + b"d", "d.jpg", None),
    ]
    before = datetime.now(UTC)
    await _batch(world, files, caption=None)
    assert await tp._tick(world) == "ok"
    after = datetime.now(UTC)
    filed, *dated = [
        c.astimezone(UTC) for (c,) in tp._sql("SELECT created_at FROM memos ORDER BY created_at")
    ]
    assert dated == [first, third]
    assert before <= filed <= after


@pytest.mark.parametrize("late_first", [True, False], ids=["late_first", "early_first"])
async def test_a_later_part_s_client_mtime_moves_an_undated_day_memo_only_earlier(
    world, monkeypatch, late_first
):
    """An attachment keeps no mtime, so a day memo's date is all that is left of what its earlier
    parts' browsers said: a later part's earlier mtime of the day moves it, a later one never
    does, whichever part is filed first."""
    late, early = NOV[0] + timedelta(hours=6), NOV[0]
    files = [(JPEG, "late.jpg", late), (JPEG + b"e", "early.jpg", early)]
    _batch_uid, batch_id = await _downloaded_in_parts(world, monkeypatch, files)
    real = trip_ingest._filing_parts
    head = "late.jpg" if late_first else "early.jpg"
    monkeypatch.setattr(
        trip_ingest,
        "_filing_parts",
        lambda blobs: sorted(real(blobs), key=lambda part: part[0].filename != head),
    )
    assert await trip_ingest._file_part(batch_id) is False
    [(created,)] = tp._sql("SELECT created_at FROM memos")
    assert created.astimezone(UTC) == (late if late_first else early)
    assert await trip_ingest._file_part(batch_id) is True
    [(created,)] = tp._sql("SELECT created_at FROM memos")
    assert created.astimezone(UTC) == early


async def test_two_pictures_of_one_name_are_filed_on_their_own_days(world):
    """iOS Safari names every picture `image.jpg`: two of them taken on 1 and 2 November are two
    memos on their own days, and a print of that name still binds to a screen of it, on the
    earliest of their days."""
    first, second = NOV[0], NOV[1]
    files = [
        (_jpeg(1, civil=second), "image.jpg", second),
        (_jpeg(2, civil=first), "image.jpg", first),
        (_jpeg(3, civil=NOV[2]), "image - print.jpg", NOV[2]),
    ]
    batch = await _batch(world, files)
    assert await tp._tick(world) == "ok"
    assert _memo_days() == [
        (date(2031, 11, 1), 1, "Beach week"),
        (date(2031, 11, 2), 1, "Beach week"),
    ]
    assert _ledger(batch) == [(date(2031, 11, 1), True), (date(2031, 11, 2), True)]
    [(screen_of_first,)] = tp._sql(
        "SELECT a.id FROM attachments a JOIN memos m ON m.id = a.memo_id "
        "ORDER BY m.created_at LIMIT 1"
    )
    assert tp._sql(
        "SELECT attachment_id FROM ingest_blobs WHERE filename = 'image - print.jpg'"
    ) == [(screen_of_first,)]
