"""MEMNG-145 — every originals move records itself BEFORE it happens, so a cancelled create
cannot leave a file where the rollback will not look for it.

Three sites move an original before the memo transaction commits: `finalize` stages an upload part
into `originals/_unfiled/`, and `_place_attachment_masters` moves the screen master and each hidden
rendition on from there into the memo folder. Each appended its rollback record AFTER awaiting the
move — and that await is exactly where a cancellation lands: anyio raises out of it while the
worker thread carries on renaming, so the file moved and the append never ran. `_restore_moved`
then had nothing to walk back, and the original sat in a staging bucket or a memo folder for a memo
that was never stored.

Each test below slows one move primitive (a dependency of the code under test, never the unit
itself), cancels the request while that move is genuinely under way, and proves both halves: the
in-stub control shows the move really happened, and the assertions show nothing survives it.
"""

import asyncio
import threading
import time
from pathlib import Path

import pytest
from app.config import settings
from app.services import storage
from app.services import uploads as up_svc
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"
SCREEN = PNG + b"screen" * 400
PRINT = PNG + b"print" * 400


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "TRUNCATE attachment_renditions, media_jobs, attachments, memo_tags, "
                "reactions, memos, uploads RESTART IDENTITY CASCADE"
            )
        )
    eng.dispose()


def _count(sql: str) -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        n = c.execute(text(sql)).scalar_one()
    eng.dispose()
    return int(n)


async def _upload(bearer, name: str, data: bytes, mime="image/png") -> str:
    opened = await bearer.post(
        "/api/uploads", json={"filename": name, "mime": mime, "size": len(data)}
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = await bearer.patch(
        f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"}
    )
    assert sent.status_code == 200, sent.text
    return uid


async def _cancel_once_started(task, started: threading.Event, what: str):
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set(), f"the {what} never started — cancelling here would prove nothing"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _stray_originals(root: Path) -> list[str]:
    originals = root / "originals"
    if not originals.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in originals.rglob("*") if p.is_file())


async def test_a_cancel_during_the_staging_move_strands_no_original(
    pat_client, monkeypatch, tmp_path
):
    """Site 1 — `finalize`: upload part → `originals/_unfiled/`."""
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    started = threading.Event()
    landed: list[bool] = []
    real_move = up_svc._move_into_sync

    def slow_move(src, dest):
        started.set()
        time.sleep(0.5)  # the window the cancellation lands in
        real_move(src, dest)
        landed.append(Path(dest).exists())  # the in-stub control, taken under the move's own lock

    monkeypatch.setattr(up_svc, "_move_into_sync", slow_move)
    try:
        uid = await _upload(bearer, "photo.png", SCREEN)
        posting = asyncio.create_task(
            bearer.post(
                "/api/memos",
                json={"content": "hung up", "visibility": "PUBLIC", "upload_uids": [uid]},
            )
        )
        await _cancel_once_started(posting, started, "staging move")

        assert landed == [True]  # the move really happened — "nothing survives" is not vacuous
        assert _stray_originals(tmp_path) == []  # …and was walked back out of _unfiled/
        assert up_svc.part_path(uid).read_bytes() == SCREEN  # the client can simply re-post
        assert _count("SELECT count(*) FROM memos") == 0
        assert _count("SELECT count(*) FROM attachments") == 0
    finally:
        _cleanup()


async def test_a_cancel_during_the_memo_folder_placement_strands_no_master(
    pat_client, monkeypatch, tmp_path
):
    """Site 2 — `_place_attachment_masters`: `originals/_unfiled/` → the memo folder."""
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    started = threading.Event()
    landed: list[bool] = []
    real_place = storage._place_master_sync

    def slow_place(src, dest_dir, name):
        started.set()
        time.sleep(0.5)
        placed = real_place(src, dest_dir, name)
        landed.append(placed.exists())
        return placed

    monkeypatch.setattr(storage, "_place_master_sync", slow_place)
    try:
        uid = await _upload(bearer, "photo.png", SCREEN)
        posting = asyncio.create_task(
            bearer.post(
                "/api/memos",
                json={"content": "hung up", "visibility": "PUBLIC", "upload_uids": [uid]},
            )
        )
        await _cancel_once_started(posting, started, "memo-folder placement")

        assert landed == [True]
        # Both hops undone, in the right order: the master is back in its part file, and neither
        # the memo folder nor _unfiled/ is holding it.
        assert _stray_originals(tmp_path) == []
        assert up_svc.part_path(uid).read_bytes() == SCREEN
        assert _count("SELECT count(*) FROM memos") == 0
        assert _count("SELECT count(*) FROM attachments") == 0
    finally:
        _cleanup()


async def test_a_cancel_during_a_renditions_placement_strands_no_master(
    pat_client, monkeypatch, tmp_path
):
    """Site 3 — the same placement, for a hidden print rendition, which is moved AFTER its screen.

    Cancelling on the second placement is what makes this a different site: by then one move has
    already completed and been recorded, so the unwind has to walk back a mixed list.
    """
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    started = threading.Event()
    placements: list[str] = []
    real_place = storage._place_master_sync

    def slow_place(src, dest_dir, name):
        if len(placements) >= 1:  # the screen went first; this is the rendition
            started.set()
            time.sleep(0.5)
        placed = real_place(src, dest_dir, name)
        placements.append(str(placed))
        return placed

    monkeypatch.setattr(storage, "_place_master_sync", slow_place)
    try:
        screen = await _upload(bearer, "Sunset.png", SCREEN)
        glossy = await _upload(bearer, "Sunset - print (glossy).png", PRINT)
        posting = asyncio.create_task(
            bearer.post(
                "/api/memos",
                json={
                    "content": "hung up",
                    "visibility": "PUBLIC",
                    "upload_uids": [screen, glossy],
                },
            )
        )
        await _cancel_once_started(posting, started, "rendition placement")

        assert len(placements) == 2, placements  # in-stub control: BOTH masters were placed
        assert _stray_originals(tmp_path) == []  # …and both were walked all the way back
        assert up_svc.part_path(screen).read_bytes() == SCREEN
        assert up_svc.part_path(glossy).read_bytes() == PRINT
        assert _count("SELECT count(*) FROM memos") == 0
        assert _count("SELECT count(*) FROM attachments") == 0
        assert _count("SELECT count(*) FROM attachment_renditions") == 0
    finally:
        _cleanup()


# --- the arbitration itself, both orders ---


def test_a_move_that_runs_first_is_recorded_and_walked_back(tmp_path):
    src = tmp_path / "parts" / "x.part"
    src.parent.mkdir(parents=True)
    src.write_bytes(SCREEN)
    dest = tmp_path / "originals" / "_unfiled" / "x.png"

    move = storage.TrackedMove(src=str(src))

    def place(source: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        return dest

    assert move.perform(place) == dest
    assert move.dest == str(dest)
    assert move.restore() is None
    assert src.read_bytes() == SCREEN  # back where it came from
    assert not dest.exists()


def test_a_restore_that_arrives_first_stops_the_move_happening_at_all(tmp_path):
    """The other order: the write unwound before the worker got its turn. The move must not run —
    a file placed after the restore has already walked the list is a file nothing will collect."""
    src = tmp_path / "parts" / "y.part"
    src.parent.mkdir(parents=True)
    src.write_bytes(SCREEN)
    dest = tmp_path / "originals" / "_unfiled" / "y.png"

    move = storage.TrackedMove(src=str(src))
    assert move.restore() is None  # nothing moved yet, so nothing to walk back

    with pytest.raises(storage.MoveAbandoned):
        move.perform(lambda source: source.rename(dest) or dest)
    assert src.read_bytes() == SCREEN
    assert not dest.exists()


def test_a_restore_reports_a_destination_it_could_not_move_back(tmp_path):
    """A restore that fails is reported, never raised: it runs while another failure is already
    unwinding, and one it cannot undo is still recoverable by hand."""
    src = tmp_path / "parts" / "z.part"
    src.parent.mkdir(parents=True)
    src.write_bytes(SCREEN)
    dest = tmp_path / "originals" / "_unfiled" / "z.png"

    move = storage.TrackedMove(src=str(src))
    move.perform(lambda source: (dest.parent.mkdir(parents=True), source.rename(dest), dest)[2])
    src.parent.chmod(0o500)  # the part directory is read-only → the rename back cannot work
    try:
        assert move.restore() == str(dest)
        assert dest.exists()  # left in place rather than lost
    finally:
        src.parent.chmod(0o700)
