"""A guest memo is dated by its pictures, not by the pull that filed it (MEMNG-1262).

A household compose sends `createdAt` (the earliest capture of its pictures); a guest batch pulled
from the trip door arrives with no such field, so it used to take the pull's own time. The filing
now dates the memo from the attachments `finalize` made — the earliest capture instant the files
carry, else the earliest client mtime, else now — and it does so BEFORE the masters are placed,
because the per-memo folder is named from `memo.created_at` once and never renamed.

Every case runs the REAL pull: door upload, home tick, `create_guest_memo`, placement on disk.
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.services import storage
from PIL import Image

from tests import test_trip_ingest_puller as tp
from tests.camera_fixtures import exif_bytes
from tests.ingest_support import JPEG, complete, upload

pluto_schema = tp.pluto_schema
world = tp.world

DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011

LATER = datetime(2031, 3, 4, 17, 45, 10, tzinfo=UTC)
EARLIER = datetime(2031, 3, 2, 8, 15, 30, tzinfo=UTC)


def _exif_jpeg(instant: datetime, colour: tuple[int, int, int]) -> bytes:
    """An 8x8 JPEG whose EXIF says when it was taken, at +02:00 so the instant is a real
    conversion and not a coincidence of the UTC wall clock."""
    local = instant + timedelta(hours=2)
    raw = exif_bytes(
        {},
        {
            DATETIME_ORIGINAL: local.strftime("%Y:%m:%d %H:%M:%S"),
            OFFSET_TIME_ORIGINAL: "+02:00",
        },
    )
    out = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(out, "JPEG", exif=raw)
    return out.getvalue()


def _ms(instant: datetime) -> int:
    return int(instant.timestamp() * 1000)


async def _file(w, *files: tuple[bytes, str, int | None]) -> tuple[datetime, str, list[Path]]:
    """Send one guest batch through the door and pull it home; the filed memo's created_at, uid
    and the placed originals."""
    trip = await tp._trip(w)
    token = await tp._guest(w, trip)
    uids = []
    for data, name, mtime in files:
        extra = {"last_modified": mtime} if mtime is not None else {}
        uids.append(await upload(w.door, token, data, name, **extra))
    r = await complete(w.door, token, uids)
    assert r.status_code in (200, 201), r.text
    assert await tp._tick(w) == "ok"
    [(memo_id, created_at, uid)] = tp._sql("SELECT id, created_at, uid FROM memos")
    # The folder is named from the instant in UTC, as the app reads it back from asyncpg.
    created_at = created_at.astimezone(UTC)
    paths = [
        Path(p)
        for (p,) in tp._sql(
            "SELECT storage_path FROM attachments WHERE memo_id = :m ORDER BY id", {"m": memo_id}
        )
    ]
    assert len(paths) == len(files)
    return created_at, uid, paths


def _in_folder_of(w, created_at: datetime, uid: str, paths: list[Path]) -> None:
    folder = storage.memo_dir(w.home_root, created_at, uid)
    for p in paths:
        assert p.parent == folder, (p, folder)
        assert p.is_file()


async def test_a_guest_memo_takes_the_earliest_exif_capture_and_its_folder_says_so(world):
    created_at, uid, paths = await _file(
        world,
        # The LATER picture first: the order of the batch does not decide. And a picture whose
        # file states no instant, with a browser mtime a month earlier still: a client mtime is
        # the fallback for a batch that states nothing, never a vote against one that does.
        (_exif_jpeg(LATER, (10, 20, 30)), "late.jpg", None),
        (JPEG, "forwarded.jpg", _ms(EARLIER - timedelta(days=30))),
        (_exif_jpeg(EARLIER, (200, 100, 50)), "early.jpg", None),
    )
    assert created_at == EARLIER
    _in_folder_of(world, created_at, uid, paths)
    assert all(p.parent.name.startswith("2031-03-02_0815_") for p in paths), paths


async def test_with_no_exif_the_earliest_client_mtime_dates_the_memo(world):
    first = datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)
    created_at, uid, paths = await _file(
        world,
        (JPEG, "a.jpg", _ms(first + timedelta(days=2))),
        (JPEG + b"\x00", "b.jpg", _ms(first)),
    )
    assert created_at == first
    _in_folder_of(world, created_at, uid, paths)


async def test_a_client_mtime_outranks_the_filing_time_even_when_it_is_later(world):
    """One file with no date at all and one whose browser mtime lies AFTER the filing: the mtime is
    still what a client said about its file, and the filing time is only for a batch that said
    nothing. A `min()` over captured_at would pick the filing time here."""
    ahead = datetime.now(UTC) + timedelta(days=40)
    ahead = ahead.replace(microsecond=0)
    created_at, uid, paths = await _file(
        world,
        (JPEG, "undated.jpg", None),
        (JPEG + b"\x00", "ahead.jpg", _ms(ahead)),
    )
    assert created_at == ahead
    _in_folder_of(world, created_at, uid, paths)


async def test_with_no_exif_and_no_mtime_the_memo_is_dated_now(world):
    before = datetime.now(UTC)
    created_at, uid, paths = await _file(world, (JPEG, "a.jpg", None))
    after = datetime.now(UTC)
    assert before - timedelta(seconds=5) <= created_at <= after
    _in_folder_of(world, created_at, uid, paths)


def test_a_household_compose_keeps_the_date_it_sends(logged_in_client, monkeypatch, tmp_path):
    """The household path is not the guest path: a compose's `created_at` is the date, whatever its
    files state, and its folder carries that date."""
    from app.config import settings

    from tests.test_memo_picture_list import _upload, _wipe

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _wipe()
    try:
        client, _ = logged_in_client
        chosen = datetime(2030, 7, 8, 9, 10, 11, tzinfo=UTC)
        up = _upload(client, "a.jpg", _exif_jpeg(EARLIER, (1, 2, 3)), "image/jpeg")
        r = client.post(
            "/api/memos",
            json={
                "content": "home",
                "visibility": "PROTECTED",
                "upload_uids": [up],
                "created_at": chosen.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        uid = r.json()["uid"]
        [(created_at, path)] = tp._sql(
            "SELECT m.created_at, a.storage_path FROM memos m JOIN attachments a "
            "ON a.memo_id = m.id WHERE m.uid = :u",
            {"u": uid},
        )
        assert created_at == chosen
        assert Path(path).parent == storage.memo_dir(tmp_path, chosen, uid)
    finally:
        _wipe()
