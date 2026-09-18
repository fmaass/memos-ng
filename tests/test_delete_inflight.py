"""Deleting an attachment while one of its media jobs is running (MEMNG-41).

A worker claims a job with SELECT … FOR UPDATE and holds that row lock until the job commits —
for a slow-lane transcode, the entire run. These tests pin the two halves of the fix:

* the DELETE never queues behind that lock (it used to, through its own job-cleanup statement AND
  through the FK's ON DELETE CASCADE, pinning a pool connection for the transcode's duration);
* the job that outlives the delete lands terminal and publishes nothing.

No real ffmpeg: the transcode command is substituted with a shell stub (same technique as
tests/test_slowlane_timeout.py), so the real subprocess machinery runs without a real encode.
"""

import asyncio
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.config import settings
from app.database import async_session
from app.models import User
from app.routers.assets import delete_attachment
from app.services import media
from app.worker import run_once
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from tests.conftest import SYNC_URL

# Generous next to the milliseconds a lock-free delete needs, and far below the seconds the stub
# transcode keeps its row lock — the gap is what makes a blocked delete a red test, not a slow one.
DELETE_BUDGET_SECONDS = 3.0
STUB_TRANSCODE_SECONDS = 4  # the window the delete has to land inside a running transcode
FILE_WAIT_SECONDS = 10.0  # ceiling on "has the stub transcode started yet?"


def _stub_transcode_cmd(src: str, dest: str, threads: int) -> list[str]:
    """Stub argv: write the partial output, then hold the run open long enough for a concurrent
    delete to land mid-transcode, then exit 0 as a successful encode would."""
    return ["sh", "-c", f'printf partial > "$1"; sleep {STUB_TRANSCODE_SECONDS}', "sh", dest]


def _seed_video(
    uid: str, *, subject: str | None = None, src: Path | None = None
) -> tuple[int, int]:
    """A committed video attachment + queued slow job (and a fast job that is already done).

    Committed on purpose: these tests need rows a *separate* connection — the worker's, or the
    lock holder's — can see and lock, which a fixture-scoped rolled-back transaction cannot give.
    """
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            if subject is None:
                subject = f"inflight-{secrets.token_urlsafe(6)}"
                c.execute(
                    text(
                        "INSERT INTO users (subject, role, default_visibility, theme) "
                        "VALUES (:s, 'user', 'PUBLIC', 'dark')"
                    ),
                    {"s": subject},
                )
            owner = c.execute(
                text("SELECT id FROM users WHERE subject=:s"), {"s": subject}
            ).scalar_one()
            aid = c.execute(
                text(
                    "INSERT INTO attachments "
                    "(uid, owner_id, filename, mime, size, status, storage_path, duration) "
                    "VALUES (:u, :o, 'v.mp4', 'video/mp4', 1, 'processing', :sp, 1.0) RETURNING id"
                ),
                {"u": uid, "o": owner, "sp": str(src) if src else "/nonexistent"},
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                    "VALUES (:a, 'fast', 'done', 0, 100)"
                ),
                {"a": aid},
            )
            jid = c.execute(
                text(
                    "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                    "VALUES (:a, 'slow', 'queued', 0, 0) RETURNING id"
                ),
                {"a": aid},
            ).scalar_one()
        return aid, jid
    finally:
        eng.dispose()


def _seed_image(uid: str) -> tuple[int, int, int]:
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            subject = f"inflight-{secrets.token_urlsafe(6)}"
            owner = c.execute(
                text(
                    "INSERT INTO users (subject, role, default_visibility, theme) "
                    "VALUES (:s, 'user', 'PUBLIC', 'dark') RETURNING id"
                ),
                {"s": subject},
            ).scalar_one()
            aid = c.execute(
                text(
                    "INSERT INTO attachments "
                    "(uid, owner_id, filename, mime, size, status, storage_path) "
                    "VALUES (:u, :o, 'p.jpg', 'image/jpeg', 1, 'pending', '/nonexistent') "
                    "RETURNING id"
                ),
                {"u": uid, "o": owner},
            ).scalar_one()
            jid = c.execute(
                text(
                    "INSERT INTO media_jobs (attachment_id, lane, state, attempts, progress_pct) "
                    "VALUES (:a, 'fast', 'queued', 0, 0) RETURNING id"
                ),
                {"a": aid},
            ).scalar_one()
        return aid, jid, owner
    finally:
        eng.dispose()


def _sql(query: str, **params):
    """Run one committed statement on its own connection (rows for a SELECT, [] otherwise)."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            result = c.execute(text(query), params)
            return result.all() if result.returns_rows else []
    finally:
        eng.dispose()


def _cleanup(aid: int, owner: int | None = None) -> None:
    # media_jobs no longer cascades from attachments (MEMNG-41) — a test that seeds job rows
    # cleans them up itself, or it leaves orphans behind for whatever runs next.
    _sql("DELETE FROM media_jobs WHERE attachment_id = :a", a=aid)
    _sql("DELETE FROM attachments WHERE id = :a", a=aid)
    if owner is not None:
        _sql("DELETE FROM users WHERE id = :o", o=owner)


def _job_row_lock_still_held(jid: int) -> bool:
    """True while another transaction still holds this job row — i.e. the 'transcode' is running.

    Asserted AFTER the delete returns: it is the direct evidence that the delete did not wait for
    the job, rather than the job having quietly finished first.
    """
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as c:
            c.execute(text("SET lock_timeout = '500ms'"))
            try:
                c.execute(text("SELECT id FROM media_jobs WHERE id=:j FOR UPDATE"), {"j": jid})
                return False
            except OperationalError:
                return True
    finally:
        eng.dispose()


async def _await_file(path: Path) -> bool:
    """Yield to the loop while waiting — a sync busy-wait here would starve the very worker task
    whose subprocess is supposed to create the file."""
    deadline = time.monotonic() + FILE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.02)
    return False


def test_delete_returns_promptly_while_a_job_row_is_locked(logged_in_client, monkeypatch, tmp_path):
    """AC1/AC2: the HTTP DELETE completes while a worker still holds the job's row lock.

    The lock is taken exactly as claim_one takes it (SELECT … FOR UPDATE, then the UPDATE to
    `processing`) on a connection that never commits — a stand-in for a transcode mid-run. The
    request runs in a worker thread with a hard deadline, so a delete that queues behind the lock
    fails the test instead of hanging it.
    """
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    uid = "inflight-http"
    aid, jid = _seed_video(uid, subject=subject)
    thumb = tmp_path / "assets" / "thumbs" / f"{uid}.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"thumb")
    eng = create_engine(SYNC_URL)
    holder = eng.connect()
    # Not a `with` block: its shutdown JOINS the worker thread, so a delete that blocks (the
    # regression) would deadlock against the lock this test only releases in `finally` — the
    # failure has to be a fast red, not a hung suite.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        holder.execute(text("SELECT id FROM media_jobs WHERE id=:j FOR UPDATE"), {"j": jid})
        holder.execute(
            text("UPDATE media_jobs SET state='processing', claimed_by='test' WHERE id=:j"),
            {"j": jid},
        )  # deliberately NOT committed: the claim and the outcome are one transaction

        started = time.monotonic()
        future = pool.submit(client.delete, f"/api/attachments/{uid}")
        response = future.result(timeout=DELETE_BUDGET_SECONDS)
        elapsed = time.monotonic() - started

        assert response.status_code == 204
        assert elapsed < DELETE_BUDGET_SECONDS
        assert _job_row_lock_still_held(jid), "the job lock was released — nothing was proven"
        assert not thumb.exists()  # files swept, as in any other delete
        rows = _sql("SELECT id, state FROM media_jobs WHERE attachment_id = :a", a=aid)
        assert [r[0] for r in rows] == [jid]  # the unlocked job went; the in-flight one stayed
        assert _sql("SELECT id FROM attachments WHERE id = :a", a=aid) == []
    finally:
        holder.rollback()  # first: frees a request still queued behind the lock, if any
        holder.close()
        eng.dispose()
        pool.shutdown(wait=False, cancel_futures=True)
        _cleanup(aid)


def test_transcode_outliving_a_delete_publishes_nothing(monkeypatch, tmp_path):
    """The other half: a real in-flight transcode whose attachment is deleted mid-run.

    Both actors run for real — worker.run_once against a stub ffmpeg, and the delete endpoint
    itself — in one event loop, on separate sessions, so the row locks are the production ones.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 60)
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _stub_transcode_cmd)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard off (dev host)
    uid = f"inflight-{secrets.token_urlsafe(4)}"
    src = tmp_path / "originals" / "v.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not really a video")
    aid, jid = _seed_video(uid, src=src)
    owner = _sql("SELECT owner_id FROM attachments WHERE id=:a", a=aid)[0][0]
    web = tmp_path / "assets" / "web"
    tmp_out = web / f"{uid}.tmp.mp4"

    async def _scenario():
        async with async_session() as worker_db:
            job = asyncio.create_task(run_once(worker_db))
            try:
                assert await _await_file(tmp_out), "the stub transcode never started"
                async with async_session() as api_db:
                    user = (await api_db.execute(select(User).where(User.id == owner))).scalar_one()
                    started = time.monotonic()
                    await asyncio.wait_for(
                        delete_attachment(uid, user, api_db), DELETE_BUDGET_SECONDS
                    )
                    elapsed = time.monotonic() - started
                transcode_still_running = not job.done()
                # Bounded too: with the fix removed the delete and the worker can wedge on each
                # other's locks, and a wedged suite is a worse signal than a failed assertion.
                status = await asyncio.wait_for(
                    asyncio.shield(job), STUB_TRANSCODE_SECONDS + DELETE_BUDGET_SECONDS + 10
                )
                return status, elapsed, transcode_still_running
            finally:
                job.cancel()

    try:
        status, elapsed, still_running = asyncio.run(_scenario())

        assert still_running, "the delete only returned after the transcode — nothing was proven"
        assert elapsed < DELETE_BUDGET_SECONDS
        assert status == "processed"  # discarded cleanly; the worker did not fail or crash
        assert not (web / f"{uid}.mp4").exists()  # never published over the delete
        assert not tmp_out.exists()  # partial swept, not left to rot
        assert _sql("SELECT id FROM media_jobs WHERE id = :j", j=jid) == []  # terminal: row gone
        assert _sql("SELECT id FROM attachments WHERE id = :a", a=aid) == []
    finally:
        _cleanup(aid, owner)


def test_job_whose_attachment_is_already_gone_is_discarded(monkeypatch, tmp_path):
    """A job that only reaches a worker AFTER the delete (it was queued, or its claim rolled back)
    is discarded on sight — no attempt counted, no derived output, no crash-looping row."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    uid = f"orphan-{secrets.token_urlsafe(4)}"
    aid, jid = _seed_video(uid)
    owner = _sql("SELECT owner_id FROM attachments WHERE id=:a", a=aid)[0][0]
    _sql("DELETE FROM attachments WHERE id = :a", a=aid)  # the delete already committed

    async def _go():
        async with async_session() as db:
            return await run_once(db)

    try:
        assert asyncio.run(_go()) == "processed"
        assert _sql("SELECT id FROM media_jobs WHERE id = :j", j=jid) == []
    finally:
        _cleanup(aid, owner)


def test_fast_lane_sweeps_its_tiers_when_the_attachment_is_deleted_mid_job(monkeypatch, tmp_path):
    """The fast lane writes its tiers before it ever touches the DB, so a delete landing during
    that work would leave thumb/mobile files behind for a row that no longer exists. The publish
    checkpoint sweeps them."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)
    uid = f"fast-{secrets.token_urlsafe(4)}"
    aid, jid, owner = _seed_image(uid)
    thumb = tmp_path / "assets" / "thumbs" / f"{uid}.jpg"
    mobile = tmp_path / "assets" / "mobile" / f"{uid}.jpg"

    def _derive_then_delete(attachment):
        # Stands in for the real Pillow work (a dependency, not the unit under test): writes the
        # same tier files, and the delete lands while it runs.
        for path in (thumb, mobile):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"tier")
        _sql("DELETE FROM attachments WHERE id = :a", a=aid)
        return {"width": 4, "height": 2, "blurhash": "LKO2"}

    monkeypatch.setattr(media, "process_image", _derive_then_delete)

    async def _go():
        async with async_session() as db:
            return await run_once(db)

    try:
        assert asyncio.run(_go()) == "processed"
        assert not thumb.exists() and not mobile.exists()  # no tiers for a deleted attachment
        assert _sql("SELECT id FROM media_jobs WHERE id = :j", j=jid) == []
    finally:
        _cleanup(aid, owner)


@pytest.mark.parametrize("tier", ["thumbs/{uid}.jpg", "web/{uid}.mp4", "web/{uid}.tmp.mp4"])
def test_guard_publish_sweeps_and_raises_when_the_row_is_gone(monkeypatch, tmp_path, tier):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    uid = f"guard-{secrets.token_urlsafe(4)}"
    aid, _jid = _seed_video(uid)
    owner = _sql("SELECT owner_id FROM attachments WHERE id=:a", a=aid)[0][0]
    path = tmp_path / "assets" / tier.format(uid=uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"derived")

    async def _go():
        async with async_session() as db:
            from app.models import Attachment, MediaJob

            att = (await db.execute(select(Attachment).where(Attachment.id == aid))).scalar_one()
            job = (await db.execute(select(MediaJob).where(MediaJob.id == _jid))).scalar_one()
            _sql("DELETE FROM attachments WHERE id = :a", a=aid)  # committed by another actor
            with pytest.raises(media.AttachmentGone):
                await media.guard_publish(db, job, att)  # job survives; the attachment is gone

    try:
        asyncio.run(_go())
        assert not path.exists()
    finally:
        _cleanup(aid, owner)
