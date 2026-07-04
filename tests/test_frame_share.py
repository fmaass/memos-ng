import pytest
from app.config import settings
from app.models import Attachment, AttachmentStatus, FrameShareJob, User, Visibility
from app.services import memos as svc
from app.services.frame_share import is_frame_image, is_valid_email, maybe_enqueue_frame_shares
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
