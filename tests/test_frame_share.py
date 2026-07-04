from datetime import UTC, datetime, timedelta

import pytest
from app.config import settings
from app.models import (
    Attachment,
    AttachmentStatus,
    FrameShareJob,
    FrameShareState,
    User,
    Visibility,
)
from app.services import memos as svc
from app.services.frame_share import (
    is_frame_image,
    is_valid_email,
    maybe_enqueue_frame_shares,
)
from sqlalchemy import func, select


def _img(owner_id: int, uid: str, mime: str = "image/jpeg") -> Attachment:
    return Attachment(
        uid=uid, owner_id=owner_id, filename=f"{uid}.jpg", mime=mime,
        size=1, status=AttachmentStatus.ready,
    )


async def _configured_user(adb, monkeypatch, subject: str = "alice") -> User:
    """A user + server config with the frame-share feature fully enabled."""
    monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-specific-pw")
    u = User(
        subject=subject, role="user",
        frame_send_to="frame@send.auraframes.com", frame_send_from="sender@me.com",
    )
    adb.add(u)
    await adb.flush()
    return u


async def _job_count(adb, memo) -> int:
    return (
        await adb.execute(
            select(func.count()).select_from(FrameShareJob).where(FrameShareJob.memo_id == memo.id)
        )
    ).scalar_one()


async def _single_job(adb, memo) -> FrameShareJob:
    return (
        await adb.execute(select(FrameShareJob).where(FrameShareJob.memo_id == memo.id))
    ).scalar_one()


async def _curated_memo_with_job(adb, monkeypatch, subject="alice"):
    """A configured user + a #curated memo with one image → exactly one enqueued (queued) job."""
    u = await _configured_user(adb, monkeypatch, subject=subject)
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1
    return u, memo, await _single_job(adb, memo)


# --- pure predicate: one case per input category (Testing Integrity: routing heuristic) ---


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("image/jpeg", True), ("image/png", True), ("image/heic", True),
        ("image/tiff", True), ("image/webp", True), ("IMAGE/JPEG", True),
        ("video/mp4", False), ("video/quicktime", False), ("application/pdf", False),
        ("image/svg+xml", False), ("", False),
    ],
)
def test_is_frame_image(mime, expected):
    assert is_frame_image(mime) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("a@b.com", True), ("first.last@send.auraframes.com", True), ("x@y.co.uk", True),
        ("", False), ("no-at-sign", False), ("no@domain", False), ("@nolocal.com", False),
        ("two@@at.com", False), ("has space@x.com", False), ("trailing@x.com ", False),
    ],
)
def test_is_valid_email(value, expected):
    assert is_valid_email(value) is expected


# --- enqueue guard: one test per input category (plan Phase 1 verification) ---


async def test_curated_images_enqueue_one_job_per_image(adb, monkeypatch):
    u = await _configured_user(adb, monkeypatch)
    adb.add_all([_img(u.id, "i1"), _img(u.id, "i2")])
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="nice shots #curated", visibility=Visibility.PRIVATE,
        attachment_uids=["i1", "i2"],
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 2
    assert await _job_count(adb, memo) == 2


async def test_curated_video_only_no_jobs(adb, monkeypatch):
    u = await _configured_user(adb, monkeypatch)
    v = Attachment(
        uid="v1", owner_id=u.id, filename="v.mp4", mime="video/mp4",
        size=1, status=AttachmentStatus.ready,
    )
    adb.add(v)
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="clip #curated", visibility=Visibility.PRIVATE, attachment_uids=["v1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    assert await _job_count(adb, memo) == 0


async def test_untagged_memo_no_jobs(adb, monkeypatch):
    u = await _configured_user(adb, monkeypatch)
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="just a note", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    assert await _job_count(adb, memo) == 0


async def test_tag_added_on_edit_enqueues(adb, monkeypatch):
    u = await _configured_user(adb, monkeypatch)
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="no tag yet", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0  # not curated
    await svc.update_memo(adb, memo, content="now curated #curated")
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1
    assert await _job_count(adb, memo) == 1


async def test_reedit_curated_no_duplicate(adb, monkeypatch):
    u = await _configured_user(adb, monkeypatch)
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1
    # Re-editing re-runs enqueue; the UNIQUE(attachment_id) conflict path inserts nothing.
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    assert await _job_count(adb, memo) == 1


async def test_new_attachment_on_curated_enqueues_only_new(adb, monkeypatch):
    u = await _configured_user(adb, monkeypatch)
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1
    # A later edit adds a second image; only the new one is enqueued.
    adb.add(_img(u.id, "i2"))
    await adb.flush()
    await svc.link_attachments(adb, u.id, memo, ["i2"])
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1
    assert await _job_count(adb, memo) == 2


# --- re-enqueue resurrection: one test per existing-row state category (P1-2) ---


async def test_skipped_job_is_resurrected(adb, monkeypatch):
    # A job that went terminal `skipped` (memo briefly un-#curated / original missing at send time)
    # must be re-enqueable: re-editing the curated memo resets it to queued so the photo retries.
    u, memo, job = await _curated_memo_with_job(adb, monkeypatch)
    job.state = FrameShareState.skipped
    job.attempts = 3
    job.error = "was missing"
    job.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)
    job.claimed_by = "worker-x"
    await adb.flush()
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1  # resurrection counts as enqueued
    await adb.refresh(job)
    assert job.state is FrameShareState.queued
    assert job.attempts == 0 and job.error is None
    assert job.next_attempt_at is None and job.claimed_by is None
    assert await _job_count(adb, memo) == 1  # resurrected in place, not duplicated


async def test_failed_job_is_resurrected(adb, monkeypatch):
    # A job that failed on bad addresses (later corrected) must get another chance on re-edit.
    u, memo, job = await _curated_memo_with_job(adb, monkeypatch)
    job.state = FrameShareState.failed
    job.attempts = 5
    job.error = "recipient refused"
    await adb.flush()
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 1
    await adb.refresh(job)
    assert job.state is FrameShareState.queued and job.attempts == 0 and job.error is None
    assert await _job_count(adb, memo) == 1


async def test_sent_job_never_resurrected(adb, monkeypatch):
    # `sent` is terminal — a re-edit must NEVER resend a photo already on the frame.
    u, memo, job = await _curated_memo_with_job(adb, monkeypatch)
    job.state = FrameShareState.sent
    job.sent_at = datetime.now(UTC)
    await adb.flush()
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    await adb.refresh(job)
    assert job.state is FrameShareState.sent  # untouched


async def test_claimed_job_never_touched(adb, monkeypatch):
    # `claimed` is the at-most-once tombstone: an in-flight send must never be reset under it.
    u, memo, job = await _curated_memo_with_job(adb, monkeypatch)
    job.state = FrameShareState.claimed
    job.claimed_by = "worker-1"
    await adb.flush()
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    await adb.refresh(job)
    assert job.state is FrameShareState.claimed and job.claimed_by == "worker-1"


async def test_queued_retry_job_untouched(adb, monkeypatch):
    # A queued job mid-backoff must keep its attempts/next_attempt_at — resurrection is only for
    # terminal rows, never a live retry (that would erase the backoff schedule).
    u, memo, job = await _curated_memo_with_job(adb, monkeypatch)
    job.attempts = 2
    future = datetime.now(UTC) + timedelta(minutes=10)
    job.next_attempt_at = future
    await adb.flush()
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    await adb.refresh(job)
    assert job.state is FrameShareState.queued and job.attempts == 2
    assert job.next_attempt_at is not None  # backoff preserved


async def test_owner_missing_settings_no_jobs(adb, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@me.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-specific-pw")
    u = User(subject="alice", role="user")  # no frame_send_to / frame_send_from
    adb.add(u)
    await adb.flush()
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    assert await _job_count(adb, memo) == 0


async def test_server_smtp_unset_no_jobs(adb, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    u = User(
        subject="alice", role="user",
        frame_send_to="frame@send.auraframes.com", frame_send_from="sender@me.com",
    )
    adb.add(u)
    await adb.flush()
    adb.add(_img(u.id, "i1"))
    await adb.flush()
    memo = await svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=["i1"]
    )
    assert await maybe_enqueue_frame_shares(adb, memo, u) == 0
    assert await _job_count(adb, memo) == 0
