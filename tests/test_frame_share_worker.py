import io
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    FrameShareJob,
    FrameShareState,
    Tag,
    User,
    Visibility,
)
from app.services import frame_share as fs
from app.services import memos as memos_svc
from PIL import Image
from sqlalchemy import delete, select

# --- render_for_frame (pure, no DB) ---


def _write_jpeg(path, size=(120, 80), color=(30, 60, 90), orientation=None, dto=None):
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    if orientation is not None:
        exif[0x0112] = orientation
    if dto is not None:
        exif.get_ifd(0x8769)[0x9003] = dto
    img.save(path, "JPEG", exif=exif)


def test_render_small_image_not_upscaled(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p, (140, 90))
    im = Image.open(io.BytesIO(fs.render_for_frame(str(p))))
    assert im.format == "JPEG"
    assert im.size == (140, 90)  # below the cap → unchanged, never upscaled


def test_render_large_image_downscaled_to_2048_longest_edge(tmp_path):
    # Real curated exports are ~8600px; render must cap the longest edge so the JPEG stays under
    # iCloud's 20MB / Aura's 9.5MB limits (regression: full-res rendered 20-46MB and iCloud 552'd).
    p = tmp_path / "big.jpg"
    _write_jpeg(p, (6000, 4000))
    out = fs.render_for_frame(str(p))
    im = Image.open(io.BytesIO(out))
    assert max(im.size) == fs.FRAME_MAX_EDGE == 2048  # longest edge capped
    assert im.size == (2048, 1365)  # aspect ratio preserved
    assert len(out) < 9 * 1024 * 1024  # safely under Aura's proxy cap


def test_render_backfills_datetimeoriginal_from_captured_at(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p)  # no EXIF DateTimeOriginal
    out = fs.render_for_frame(str(p), datetime(2024, 3, 1, 12, 30, 0, tzinfo=UTC))
    dto = Image.open(io.BytesIO(out)).getexif().get_ifd(0x8769).get(0x9003)
    assert dto == "2024:03:01 12:30:00"


def test_render_preserves_existing_datetimeoriginal(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p, dto="2020:01:02 03:04:05")
    out = fs.render_for_frame(str(p), datetime(2024, 3, 1, tzinfo=UTC))
    dto = Image.open(io.BytesIO(out)).getexif().get_ifd(0x8769).get(0x9003)
    assert dto == "2020:01:02 03:04:05"  # source EXIF wins; captured_at does not overwrite


def test_render_normalizes_orientation(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p, size=(100, 60), orientation=6)  # 6 = rotate 90° CW
    im = Image.open(io.BytesIO(fs.render_for_frame(str(p))))
    assert im.getexif().get(0x0112, 1) == 1  # tag reset
    assert im.size == (60, 100)  # pixels physically rotated by exif_transpose


def test_render_heic_source_to_jpeg(tmp_path):
    pytest.importorskip("pillow_heif")
    p = tmp_path / "o.heic"
    try:
        Image.new("RGB", (80, 50), (5, 5, 5)).save(p, format="HEIF")
    except (OSError, KeyError):
        pytest.skip("HEIF encoder unavailable in this pillow-heif build")
    assert Image.open(io.BytesIO(fs.render_for_frame(str(p)))).format == "JPEG"


def test_render_test_image_uses_the_real_render_path():
    # DateTimeOriginal is injected only by render_for_frame's captured_at backfill — its presence
    # proves render_test_image goes through the real renderer, not a shortcut JPEG.
    im = Image.open(io.BytesIO(fs.render_test_image()))
    assert im.format == "JPEG"
    assert im.getexif().get_ifd(0x8769).get(0x9003) is not None


# --- send_frame_share ---


async def test_send_builds_one_jpeg_attachment_over_starttls(monkeypatch):
    captured = {}

    async def fake_send(msg, **kw):
        captured["msg"] = msg
        captured["kw"] = kw
        return ({}, "250 ok")

    monkeypatch.setattr(fs.aiosmtplib, "send", fake_send)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example")
    await fs.send_frame_share(
        from_addr="a@me.com", to_addr="f@send.auraframes.com",
        filename="x.jpg", jpeg_bytes=b"\xff\xd8\xff\xd9",
    )
    msg = captured["msg"]
    atts = list(msg.iter_attachments())
    assert len(atts) == 1 and atts[0].get_content_type() == "image/jpeg"
    assert msg["From"] == "a@me.com" and msg["To"] == "f@send.auraframes.com"
    assert captured["kw"]["start_tls"] is True and captured["kw"]["hostname"] == "smtp.example"
    # Explicit send timeout (not aiosmtplib's 60s default) so a hung SMTP server can't stall the
    # worker drain long enough to flap the healthcheck.
    assert captured["kw"]["timeout"] == 30


# --- process_claimed_job state machine (adb; the worker owns commits, so no commit here) ---


async def _make_claimed(
    adb, monkeypatch, tmp_path, *, content="pics #curated", mime="image/jpeg",
    write_file=True, configured=True, subject=None,
):
    if configured:
        monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@me.com")
        monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-pw")
    u = User(
        subject=subject or f"u-{secrets.token_hex(4)}", role="user",
        frame_send_to="frame@send.auraframes.com", frame_send_from="sender@me.com",
    )
    adb.add(u)
    await adb.flush()
    storage = None
    if write_file:
        f = tmp_path / f"{secrets.token_hex(4)}.jpg"
        _write_jpeg(f, (64, 48))
        storage = str(f)
    att = Attachment(
        uid=secrets.token_urlsafe(8), owner_id=u.id, filename="photo.jpg", mime=mime,
        size=1, status=AttachmentStatus.ready, storage_path=storage,
        captured_at=datetime(2024, 5, 5, tzinfo=UTC),
    )
    adb.add(att)
    await adb.flush()
    memo = await memos_svc.create_memo(
        adb, u, content=content, visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
    )
    job = FrameShareJob(
        attachment_id=att.id, memo_id=memo.id, user_id=u.id,
        state=FrameShareState.claimed, attempts=0,
    )
    adb.add(job)
    await adb.flush()
    return job, att, memo, u


def _record_send(monkeypatch, calls, exc=None):
    async def fake_send(msg, **kw):
        calls.append(msg)
        if exc is not None:
            raise exc
        return ({}, "250 ok")

    monkeypatch.setattr(fs.aiosmtplib, "send", fake_send)


async def test_process_success_marks_sent(adb, monkeypatch, tmp_path):
    calls = []
    _record_send(monkeypatch, calls)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "sent"
    assert job.state is FrameShareState.sent and job.sent_at is not None
    assert len(calls) == 1


async def test_process_stamps_memo_date_not_captured_at(adb, monkeypatch, tmp_path):
    # Frame ordering key is the MEMO's date, not attachment.captured_at (unreliable for exports).
    captured = {}

    async def fake_send(msg, **kw):
        captured["msg"] = msg
        return ({}, "250 ok")

    monkeypatch.setattr(fs.aiosmtplib, "send", fake_send)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "s@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pw")
    u = User(
        subject=f"u-{secrets.token_hex(4)}", role="user",
        frame_send_to="f@send.auraframes.com", frame_send_from="s@me.com",
    )
    adb.add(u)
    await adb.flush()
    src = tmp_path / "src.png"
    Image.new("RGB", (50, 40), (1, 2, 3)).save(src, "PNG")  # PNG source → no EXIF
    att = Attachment(
        uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/png",
        size=1, status=AttachmentStatus.ready, storage_path=str(src),
        captured_at=datetime(2020, 1, 1, tzinfo=UTC),  # deliberately WRONG/old
    )
    adb.add(att)
    await adb.flush()
    memo = await memos_svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid],
        created_at=datetime(2026, 5, 12, 9, 30, 0, tzinfo=UTC),  # the intended frame date
    )
    job = FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                        state=FrameShareState.claimed, attempts=0)
    adb.add(job)
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "sent"
    part = next(captured["msg"].iter_attachments())
    im = Image.open(io.BytesIO(part.get_payload(decode=True)))
    dto = im.getexif().get_ifd(0x8769).get(0x9003)
    assert dto == "2026:05:12 09:30:00"  # memo date, NOT captured_at's 2020


async def test_process_transient_smtp_retries_with_backoff(adb, monkeypatch, tmp_path):
    _record_send(monkeypatch, [], exc=fs.aiosmtplib.SMTPServerDisconnected("conn lost"))
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "retry"
    assert job.state is FrameShareState.queued
    assert job.attempts == 1 and job.next_attempt_at is not None


async def test_process_transient_terminal_at_max_attempts(adb, monkeypatch, tmp_path):
    _record_send(monkeypatch, [], exc=fs.aiosmtplib.SMTPServerDisconnected("conn lost"))
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    job.attempts = 4  # next failure is the 5th
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "failed"
    assert job.state is FrameShareState.failed and job.attempts == 5


async def test_process_permanent_smtp_fails_immediately(adb, monkeypatch, tmp_path):
    _record_send(monkeypatch, [], exc=fs.aiosmtplib.SMTPAuthenticationError(535, "bad creds"))
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "failed"
    # Terminal on the FIRST attempt — no churn through all five.
    assert job.state is FrameShareState.failed and job.attempts == 1


async def test_process_render_failure_fails_immediately(adb, monkeypatch, tmp_path):
    # A corrupt/undecodable original makes render_for_frame raise. That is a permanent data problem,
    # NOT a transient send error — it must go terminal `failed` on the FIRST attempt, never churn
    # through all five with backoff, and never reach SMTP.
    calls = []
    _record_send(monkeypatch, calls)
    job, att, *_ = await _make_claimed(adb, monkeypatch, tmp_path, write_file=False)
    bad = tmp_path / "corrupt.jpg"
    bad.write_bytes(b"this is not a decodable image")
    att.storage_path = str(bad)
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "failed"
    assert job.state is FrameShareState.failed
    assert job.attempts == 1  # terminal on first attempt, no 5-attempt churn
    assert job.next_attempt_at is None  # no retry scheduled
    assert calls == []  # never reached the send step
    assert job.error  # the decode error is recorded


async def test_process_uncurated_skips_without_sending(adb, monkeypatch, tmp_path):
    calls = []
    _record_send(monkeypatch, calls)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, content="no tag here")
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and calls == []


async def test_process_unlinked_attachment_skips(adb, monkeypatch, tmp_path):
    calls = []
    _record_send(monkeypatch, calls)
    job, att, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    att.memo_id = None  # attachment unlinked before the worker ran
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and calls == []


async def test_process_missing_original_file_skips(adb, monkeypatch, tmp_path):
    calls = []
    _record_send(monkeypatch, calls)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, write_file=False)
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and calls == []


async def test_process_server_smtp_unset_skips(adb, monkeypatch, tmp_path):
    calls = []
    _record_send(monkeypatch, calls)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, configured=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and calls == []


# --- run_frame_share_drain isolates errors from the worker loop (media stays alive) ---


async def test_frame_share_drain_swallows_errors(monkeypatch, db_engine):
    # A raise out of run_frame_share_once (a DB error in claim/revalidate/commit) MUST NOT escape
    # the drain — if it did, run()'s loop would crash and media processing would halt. The drain
    # converts it to a non-processed 'error' status and continues.
    async def boom(db):
        raise RuntimeError("db exploded mid-claim")

    monkeypatch.setattr(worker, "run_frame_share_once", boom)
    assert await worker.run_frame_share_drain() == "error"


# --- claim honors state + next_attempt_at ---


async def test_claim_frame_share_honors_state_and_next_attempt(adb, monkeypatch, tmp_path):
    due, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    future, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    already_sent, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    # Reset states: `due` is queued+due, `future` is queued but not yet due, `already_sent` is sent.
    due.state = FrameShareState.queued
    future.state = FrameShareState.queued
    future.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
    already_sent.state = FrameShareState.sent
    await adb.flush()
    claimed = await worker.claim_frame_share_one(adb)
    assert claimed is not None and claimed.id == due.id
    assert claimed.state is FrameShareState.claimed and claimed.claimed_by is not None


# --- integration: run_frame_share_once end-to-end with real commits (at-most-once path) ---


async def test_run_frame_share_once_end_to_end_marks_sent(monkeypatch, tmp_path, db_engine):
    calls = []
    _record_send(monkeypatch, calls)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-pw")
    f = tmp_path / "real.jpg"
    _write_jpeg(f, (100, 70))
    subject = f"e2e-{secrets.token_hex(4)}"
    user_id = None
    try:
        async with async_session() as db:
            u = User(
                subject=subject, role="user",
                frame_send_to="frame@send.auraframes.com", frame_send_from="sender@me.com",
            )
            db.add(u)
            await db.flush()
            user_id = u.id
            att = Attachment(
                uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/jpeg",
                size=1, status=AttachmentStatus.ready, storage_path=str(f),
            )
            db.add(att)
            await db.flush()
            memo = await memos_svc.create_memo(
                db, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
            )
            db.add(FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                                 state=FrameShareState.queued, attempts=0))
            await db.commit()

        async with async_session() as db:
            assert await worker.run_frame_share_once(db) == "processed"

        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            assert job.state is FrameShareState.sent and job.sent_at is not None
        assert len(calls) == 1
    finally:
        # This test COMMITS (unlike adb-rolled-back tests), so it must remove everything it wrote.
        # Deleting the user cascades to memo/attachment/job, but the shared `tags` row does not
        # cascade — a leftover 'curated' tag would collide with test_models' UNIQUE Tag insert.
        async with async_session() as db:
            if user_id is not None:
                await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tag).where(Tag.name == "curated"))
            await db.commit()


async def test_unconfigured_worker_leaves_frame_jobs_queued(monkeypatch, tmp_path, db_engine):
    # The Merkur slow-lane worker runs the same image with no SMTP creds. It must NOT claim
    # frame-share jobs (which it couldn't send) — it leaves them queued for the Saturn worker.
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    f = tmp_path / "real.jpg"
    _write_jpeg(f, (100, 70))
    subject = f"guard-{secrets.token_hex(4)}"
    user_id = None
    try:
        async with async_session() as db:
            u = User(
                subject=subject, role="user",
                frame_send_to="frame@send.auraframes.com", frame_send_from="me@me.com",
            )
            db.add(u)
            await db.flush()
            user_id = u.id
            att = Attachment(
                uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/jpeg",
                size=1, status=AttachmentStatus.ready, storage_path=str(f),
            )
            db.add(att)
            await db.flush()
            memo = await memos_svc.create_memo(
                db, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
            )
            db.add(FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                                 state=FrameShareState.queued, attempts=0))
            await db.commit()

        async with async_session() as db:
            assert await worker.run_frame_share_once(db) == "idle"

        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            assert job.state is FrameShareState.queued  # NOT claimed, NOT skipped
    finally:
        async with async_session() as db:
            if user_id is not None:
                await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tag).where(Tag.name == "curated"))
            await db.commit()
