from datetime import UTC, datetime

import pytest
from app.models import (
    Attachment,
    AttachmentStatus,
    JobLane,
    JobState,
    MediaJob,
    Memo,
    Pat,
    PushSubscription,
    Tag,
    Upload,
    User,
    Visibility,
    WebSession,
)
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError


def _user(db, subject="alice"):
    u = User(subject=subject, display_name="Alice", email="a@example.com", role="user")
    db.add(u)
    db.flush()
    return u


def test_migration_creates_all_expected_tables(db_engine):
    tables = set(inspect(db_engine).get_table_names())
    expected = {
        "users",
        "memos",
        "tags",
        "memo_tags",
        "attachments",
        "uploads",
        "media_jobs",
        "sessions",
        "pats",
        "push_subscriptions",
    }
    assert expected <= tables, expected - tables


def test_uid_columns_are_unique_indexed(db_engine):
    insp = inspect(db_engine)
    for table in ("memos", "attachments"):
        uniq = [i for i in insp.get_indexes(table) if i["unique"] and i["column_names"] == ["uid"]]
        assert uniq, f"{table}.uid is not unique-indexed"


def test_user_memo_roundtrip(db_session):
    u = _user(db_session)
    db_session.add(
        Memo(uid="abc123", owner_id=u.id, content="#hello world", visibility=Visibility.PUBLIC)
    )
    db_session.flush()
    got = db_session.execute(select(Memo).where(Memo.uid == "abc123")).scalar_one()
    assert got.content == "#hello world"
    assert got.visibility == Visibility.PUBLIC
    assert got.owner_id == u.id
    assert got.pinned is False
    assert got.created_at is not None  # server_default populated


def test_memo_uid_unique(db_session):
    u = _user(db_session, "bob")
    db_session.add(Memo(uid="dup", owner_id=u.id, content="a", visibility=Visibility.PRIVATE))
    db_session.flush()
    db_session.add(Memo(uid="dup", owner_id=u.id, content="b", visibility=Visibility.PRIVATE))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_attachment_uid_unique_and_status_enum(db_session):
    db_session.add(
        Attachment(
            uid="att1",
            filename="IMG.jpg",
            mime="image/jpeg",
            size=100,
            status=AttachmentStatus.pending,
        )
    )
    db_session.flush()
    got = db_session.execute(select(Attachment).where(Attachment.uid == "att1")).scalar_one()
    assert got.status == AttachmentStatus.pending
    db_session.add(Attachment(uid="att1", filename="x", mime="image/jpeg", size=1))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_captured_at_preserved(db_session):
    ts = datetime(2021, 7, 4, 12, 30, tzinfo=UTC)
    db_session.add(
        Attachment(uid="att2", filename="v.mov", mime="video/quicktime", size=999, captured_at=ts)
    )
    db_session.flush()
    got = db_session.execute(select(Attachment).where(Attachment.uid == "att2")).scalar_one()
    assert got.captured_at == ts


def test_media_job_enums_persist(db_session):
    att = Attachment(uid="att3", filename="v.mov", mime="video/quicktime", size=1)
    db_session.add(att)
    db_session.flush()
    db_session.add(MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued))
    db_session.flush()
    job = db_session.execute(select(MediaJob).where(MediaJob.attachment_id == att.id)).scalar_one()
    assert job.lane == JobLane.slow and job.state == JobState.queued and job.attempts == 0


def test_memo_tags_association(db_session):
    u = _user(db_session, "carol")
    m = Memo(uid="m-tag", owner_id=u.id, content="#curated", visibility=Visibility.PUBLIC)
    t = Tag(name="curated")
    m.tags.append(t)
    db_session.add(m)
    db_session.flush()
    got = db_session.execute(select(Memo).where(Memo.uid == "m-tag")).scalar_one()
    assert [tag.name for tag in got.tags] == ["curated"]


def test_uploads_and_push_subscriptions_roundtrip(db_session):
    db_session.add(
        Upload(
            upload_uid="up1", filename="big.mov", mime="video/quicktime", declared_size=200_000_000
        )
    )
    u = _user(db_session, "dave")
    db_session.add(PushSubscription(user_id=u.id, endpoint="https://push/1", p256dh="k", auth="a"))
    db_session.add(
        WebSession(
            sid="s1",
            user_id=u.id,
            encrypted_tokens="enc",
            absolute_expiry=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    db_session.add(Pat(hashed_token="h1", name="cli", user_id=u.id))
    db_session.flush()
    assert (
        db_session.execute(select(Upload).where(Upload.upload_uid == "up1"))
        .scalar_one()
        .bytes_received
        == 0
    )
    assert (
        db_session.execute(select(PushSubscription).where(PushSubscription.user_id == u.id))
        .scalar_one()
        .endpoint
        == "https://push/1"
    )
