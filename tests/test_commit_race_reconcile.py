"""MEMNG-146 — the one window in POST /api/memos that no in-request bookkeeping can close.

A failure delivered INSIDE `db.commit()` is ambiguous: the server may have committed while the
driver raises. The handler's `committed` flag stays False, so its cleanup runs against a memo that
IS stored — it discards the poster of a live video and moves a live memo's originals back out of
its folder. That state is produced here for real (the commit runs, then raises, exactly as a lost
connection would) rather than hand-assembled, so the test breaks if the handler's unwind changes.

The sweep then has to find both shapes, put back the one it can, and be safe to run again.
"""

import io
from pathlib import Path

import pytest
from app.config import settings
from app.models import JobLane, JobState, MediaJob, Memo
from app.services import poster
from app.services.reconcile import reconcile_commit_races
from PIL import Image
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import SYNC_URL

VIDEO_BYTES = bytes(range(256)) * 400


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (12, 34, 56)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def _finish(adb):
    """Let go of the sweep's own transaction BEFORE cleaning up.

    These tests read and write rows the app committed, so `adb` holds locks on them; a cleanup on
    another connection would wait for a transaction that only ends at fixture teardown, which is
    the test's own deadlock. Rolling the savepoint back first releases them (and discards the
    sweep's writes, which is what we want anyway).
    """
    await adb.rollback()
    _cleanup()


def _cleanup():
    """DELETE, not TRUNCATE: TRUNCATE takes ACCESS EXCLUSIVE, which would queue behind any reader
    still holding a transaction on these tables."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for table in ("media_jobs", "attachment_renditions", "attachments", "memos", "uploads"):
            c.execute(text(f"DELETE FROM {table}"))
    eng.dispose()


def _one(sql: str):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        value = c.execute(text(sql)).scalar_one()
    eng.dispose()
    return value


async def _upload(bearer, name: str, mime: str, data: bytes) -> str:
    opened = await bearer.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = await bearer.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    return uid


def _race_the_commit(monkeypatch) -> list[bool]:
    """Make the FIRST commit that is storing a memo succeed and then raise, which is what a
    connection lost inside COMMIT looks like to the driver. Returns the in-test control list: it
    must be non-empty afterwards, or the race never happened and the test proves nothing.

    Discriminated by the session's identity map so the PAT last-used stamp's own commit, which
    rides a separate background session, cannot consume the one shot.
    """
    real_commit = AsyncSession.commit
    fired: list[bool] = []

    async def racing_commit(self: AsyncSession) -> None:
        storing_a_memo = any(
            isinstance(obj, Memo) for obj in self.sync_session.identity_map.values()
        )
        await real_commit(self)
        if storing_a_memo and not fired:
            fired.append(True)
            raise RuntimeError("connection lost during COMMIT")

    monkeypatch.setattr(AsyncSession, "commit", racing_commit)
    return fired


async def test_the_sweep_recovers_a_commit_raced_create_and_is_safe_to_repeat(
    pat_client, adb, monkeypatch, tmp_path
):
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        video_uid = await _upload(bearer, "clip.mov", "video/quicktime", VIDEO_BYTES)
        poster_uid = await _upload(bearer, "title.jpg", "image/jpeg", _jpeg())

        fired = _race_the_commit(monkeypatch)
        with pytest.raises(RuntimeError):
            await bearer.post(
                "/api/memos",
                json={
                    "content": "raced its own commit",
                    "visibility": "PUBLIC",
                    "upload_uids": [video_uid],
                    "video_posters": {video_uid: poster_uid},
                },
            )
        monkeypatch.undo()
        monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
        assert fired == [True], "the commit never raced — this test would prove nothing"

        # The state the ambiguity leaves: the memo IS stored, and the handler's unwind ran anyway.
        assert _one("SELECT count(*) FROM memos") == 1
        assert _one("SELECT count(*) FROM attachments") == 1
        uid = _one("SELECT uid FROM attachments")
        stored_path = _one("SELECT storage_path FROM attachments")
        assert not poster.poster_path(uid).exists()  # the live video's poster was discarded
        assert not Path(stored_path).exists()  # …and its original was moved back out

        # The create's own fast-lane job committed with the memo, so in THIS interleaving the
        # pipeline still owns the poster. The sweep must leave it alone — a second job would only
        # double the transcode work on a thermally-bound NAS.
        first = await reconcile_commit_races(adb)
        assert first.posters_requeued == []
        assert first.misplaced_originals == [uid]  # reported, never guessed at
        assert first.scanned == 1 and first.truncated is False
        assert len((await adb.execute(select(MediaJob))).scalars().all()) == 1

        # The other interleaving, and the one that does NOT self-heal: the worker reached the fast
        # lane before the unwind ran, so the poster it wrote was discarded by a request whose job
        # is already done and will never run again. Nothing left in the pipeline names this video.
        await adb.execute(update(MediaJob).values(state=JobState.done))
        second = await reconcile_commit_races(adb)
        assert second.posters_requeued == [uid]  # a hero frame will be derived again
        assert second.misplaced_originals == [uid]

        queued = (
            (
                await adb.execute(
                    select(MediaJob).where(
                        MediaJob.state == JobState.queued, MediaJob.lane == JobLane.fast
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(queued) == 1

        # Idempotent: the third sweep sees the job the second one queued and adds nothing.
        third = await reconcile_commit_races(adb)
        assert third.posters_requeued == []
        assert third.misplaced_originals == [uid]  # still broken, still reported
        assert len((await adb.execute(select(MediaJob))).scalars().all()) == 2
    finally:
        await _finish(adb)


async def test_a_healthy_estate_gives_the_sweep_nothing_to_do(
    pat_client, adb, monkeypatch, tmp_path
):
    """The control for the test above: the same create, uninterrupted. A sweep that reported these
    would be reporting every video in the library."""
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        video_uid = await _upload(bearer, "clip.mov", "video/quicktime", VIDEO_BYTES)
        poster_uid = await _upload(bearer, "title.jpg", "image/jpeg", _jpeg())
        made = await bearer.post(
            "/api/memos",
            json={
                "content": "an ordinary create",
                "visibility": "PUBLIC",
                "upload_uids": [video_uid],
                "video_posters": {video_uid: poster_uid},
            },
        )
        assert made.status_code == 201, made.text
        uid = made.json()["attachments"][0]["uid"]
        assert poster.poster_path(uid).exists()

        report = await reconcile_commit_races(adb)
        assert report.posters_requeued == []
        assert report.misplaced_originals == []
        assert report.scanned == 1
    finally:
        await _finish(adb)


async def test_the_scan_is_bounded_and_says_when_it_stopped_short(
    pat_client, adb, monkeypatch, tmp_path
):
    """The cap has to be visible: a truncated sweep that looked identical to a complete one would
    hide half the estate from every future run."""
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uids = []
        for i in range(2):
            up = await _upload(bearer, f"p{i}.png", "image/png", b"\x89PNG\r\n\x1a\n" + bytes(400))
            made = await bearer.post(
                "/api/memos",
                json={"content": f"m{i}", "visibility": "PUBLIC", "upload_uids": [up]},
            )
            assert made.status_code == 201, made.text
            uids.append(made.json()["attachments"][0]["uid"])

        report = await reconcile_commit_races(adb, limit=1)
        assert report.scanned == 1 and report.truncated is True
        full = await reconcile_commit_races(adb, limit=10)
        assert full.scanned == 2 and full.truncated is False
    finally:
        await _finish(adb)
