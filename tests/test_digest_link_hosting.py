"""Link mode hosts every picture and every video poster, and files live seven days from the day's
FIRST publication (MEMNG-1244, DD-1 S1.1).

These tests COMMIT, like the runner's own. Three systems are faked and only those: SMTP
(`FakeTransport`), Dropbox (`FakeHost`) and the door (`StampingPublisher`, the `DayPublisher`
seam MEMNG-1247 fills). The selection, the ledger, the encoder, the stamping and the sweep are the
real ones.
"""

import asyncio
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import User, Visibility
from app.services.digest import hosting, metrics, publish
from app.services.digest import runner as digest_runner
from sqlalchemy import create_engine, delete, text

from tests.conftest import SYNC_URL
from tests.digest_support import (
    FakeHost,
    FakeTransport,
    add_recipients,
    delivery_rows,
    group_rows,
    host_factory_for,
    hosted_rows,
    insert_hosted_row,
    reset_digest_state,
    run_rows,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_mobile,
    write_poster,
    write_web_mp4,
)

SENDER = "sender@example.com"
ENGLISH = ["carol@example.com"]
PAGE_HOST = "dami.discomarder.live"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 Europe/Zurich
DAY = date(2026, 6, 30)
# What the door mints on the day's FIRST publication. Deliberately not NOW: the stamp must come
# from the door's clock, never from the run's.
FIRST_PUBLISHED = datetime(2026, 7, 1, 6, 0, 17, tzinfo=UTC)


@dataclass
class StampingPublisher:
    """The door: one page per day, `published_at` minted once per day and kept on re-publication
    — the door's contract (Window). `refuse` makes it unreachable, the publish failure."""

    published_at: datetime = FIRST_PUBLISHED
    refuse: bool = False
    calls: list[list[date]] = field(default_factory=list)
    minted: dict[date, datetime] = field(default_factory=dict)

    async def publish(self, days: Sequence[date]) -> list[publish.DayPublication]:
        self.calls.append(list(days))
        if self.refuse:
            raise publish.PublisherUnavailable("door unreachable")
        out = []
        for day in days:
            published_at = self.minted.setdefault(day, self.published_at)
            out.append(
                publish.DayPublication(
                    day=day,
                    url=f"https://{PAGE_HOST}/d/{day.isoformat()}",
                    online_until=published_at + timedelta(days=7),
                    published_at=published_at,
                )
            )
        return out


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(app_settings, "DAILY_PAGE_HOST", PAGE_HOST)
    set_digest_settings(
        enabled=True,
        start_date="2026-06-01",
        grace_minutes=30,
        delivery_mode="link",
        hosting_enabled=True,
        retention_days=7,
    )
    subject = f"digest-hosting-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        metrics.set_snapshot(None)
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed(subject, data_root, *, photos: int, videos: int, created_at=None):
    """One PUBLIC memo with `photos` photos and `videos` videos. Returns (photo ids, video ids)."""
    async with async_session() as db:
        user = await seed_user(db, subject=f"{subject}-{secrets.token_hex(2)}")
        memo = await seed_memo(
            db,
            user,
            content="A day out",
            visibility=Visibility.PUBLIC,
            created_at=created_at or datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
        )
        photo_ids, video_ids = [], []
        for _ in range(photos):
            attachment = await seed_attachment(db, user, memo, mime="image/jpeg")
            write_mobile(data_root, attachment.uid, size=(2048, 1536), noise=True)
            photo_ids.append(attachment.id)
        for _ in range(videos):
            attachment = await seed_attachment(
                db, user, memo, mime="video/mp4", filename="clip.mp4"
            )
            write_web_mp4(data_root, attachment.uid)
            write_poster(data_root, attachment.uid, noise=True)
            video_ids.append(attachment.id)
        await db.commit()
    return photo_ids, video_ids


def _sql(statement: str, **binds) -> list[dict]:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        result = conn.execute(text(statement), binds)
        rows = [dict(row) for row in result.mappings()] if result.returns_rows else []
    engine.dispose()
    return rows


def _expiries() -> dict[tuple[int, str], datetime | None]:
    return {
        (row["attachment_id"], row["kind"]): row["expires_at"]
        for row in _sql(
            "SELECT attachment_id, kind, expires_at FROM digest_hosted_media "
            "WHERE status <> 'swept' ORDER BY id"
        )
    }


def _kinds() -> list[tuple[int, str]]:
    return sorted((row["attachment_id"], row["kind"]) for row in hosted_rows())


# --- AC 1: the ledger answers BY KIND -------------------------------------------------------------


async def test_a_video_with_mp4_and_poster_rows_yields_two_distinct_links_by_kind(env):
    subject, data_root = env
    _, (video_id,) = await _seed(subject, data_root, photos=0, videos=1)
    insert_hosted_row(
        attachment_id=video_id,
        kind="video",
        dropbox_path="/memos-mail/a/clip.mp4",
        shared_link="https://dropbox.example/clip.mp4?raw=1",
        uploaded_at=datetime(2026, 6, 30, 7, tzinfo=UTC),
    )
    # NEWER than the mp4: under a lookup keyed on the attachment alone it is the one that wins.
    insert_hosted_row(
        attachment_id=video_id,
        kind="poster",
        dropbox_path="/memos-mail/b/clip.poster.jpg",
        shared_link="https://dropbox.example/clip.poster.jpg?raw=1",
        uploaded_at=datetime(2026, 6, 30, 8, tzinfo=UTC),
    )

    async with async_session() as db:
        links = await digest_runner.existing_hosted_links(db, [video_id])

    assert links == {
        (video_id, hosting.VIDEO): "https://dropbox.example/clip.mp4?raw=1",
        (video_id, hosting.POSTER): "https://dropbox.example/clip.poster.jpg?raw=1",
    }


async def test_attachments_mode_still_links_the_mp4_when_a_newer_poster_row_exists(env):
    subject, data_root = env
    set_digest_settings(delivery_mode="attachments")
    add_recipients(*ENGLISH, language="en")
    _, (video_id,) = await _seed(subject, data_root, photos=0, videos=1)
    insert_hosted_row(
        attachment_id=video_id,
        kind="video",
        dropbox_path="/memos-mail/a/clip.mp4",
        shared_link="https://dropbox.example/clip.mp4?raw=1",
        uploaded_at=datetime(2026, 6, 30, 7, tzinfo=UTC),
    )
    insert_hosted_row(
        attachment_id=video_id,
        kind="poster",
        dropbox_path="/memos-mail/b/clip.poster.jpg",
        shared_link="https://dropbox.example/clip.poster.jpg?raw=1",
        uploaded_at=datetime(2026, 6, 30, 8, tzinfo=UTC),
    )

    rendered, _ = await digest_runner.preview(now_utc=NOW, language="en")

    assert rendered.links.by_attachment[video_id] == "https://dropbox.example/clip.mp4?raw=1"


# --- AC 2: one image_mail per photo, one poster per video, idempotent per kind -------------------


async def test_a_link_mode_run_uploads_one_image_mail_per_photo_and_one_poster_per_video(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    photo_ids, (video_id,) = await _seed(subject, data_root, photos=2, videos=1)
    host = FakeHost()

    result = await digest_runner.run_digest(
        transport=FakeTransport(),
        host_factory=host_factory_for(host),
        publisher=StampingPublisher(),
        now_utc=NOW,
    )

    assert result is not None and result.status == digest_runner.SENT
    assert _kinds() == sorted(
        [(photo_id, hosting.IMAGE_MAIL) for photo_id in photo_ids]
        + [(video_id, hosting.POSTER), (video_id, hosting.VIDEO)]
    )
    assert all(row["status"] == "uploaded" for row in hosted_rows())
    # The picture is the 1600 px MAIL profile, not the 2048 px tier it is encoded from.
    mail_uploads = [path for path, name in host.uploads if name.endswith(".mail.jpg")]
    assert len(mail_uploads) == 2


async def test_the_upload_pass_is_idempotent_per_kind_and_a_hosted_mp4_does_not_hide_the_poster(
    env,
):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    (photo_id,), (video_id,) = await _seed(subject, data_root, photos=1, videos=1)
    # An earlier attachments-mode run already hosted this video's mp4.
    insert_hosted_row(
        attachment_id=video_id,
        kind="video",
        dropbox_path="/memos-mail/earlier/clip.mp4",
        shared_link="https://dropbox.example/clip.mp4?raw=1",
        uploaded_at=datetime(2026, 6, 30, 7, tzinfo=UTC),
    )
    host = FakeHost()
    door = StampingPublisher(refuse=True)

    first = await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), publisher=door, now_utc=NOW
    )
    assert first is not None and first.status == digest_runner.DEFERRED
    uploaded_once = sorted(name for _, name in host.uploads)
    # The poster is a kind of its own: the hosted mp4 does not satisfy it, and the mp4 is reused.
    assert sorted(name.split(".", 1)[1] for name in uploaded_once) == ["mail.jpg", "poster.jpg"]

    door.refuse = False
    second = await digest_runner.run_digest(
        transport=FakeTransport(),
        host_factory=host_factory_for(host),
        publisher=door,
        now_utc=NOW + timedelta(days=1),
    )

    assert second is not None and second.status == digest_runner.SENT
    assert sorted(name for _, name in host.uploads) == uploaded_once  # nothing uploaded twice
    assert _kinds() == sorted(
        [(photo_id, hosting.IMAGE_MAIL), (video_id, hosting.POSTER), (video_id, hosting.VIDEO)]
    )


# --- AC 3: the window is stamped at publish, from the FIRST publication ---------------------------


async def test_a_reused_upload_whose_publish_failed_is_unstamped_until_the_next_publish(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    await _seed(subject, data_root, photos=2, videos=1)
    host = FakeHost()
    door = StampingPublisher(refuse=True)

    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(host), publisher=door, now_utc=NOW
    )
    assert _expiries() and set(_expiries().values()) == {None}
    assert _sql("SELECT * FROM daily_pushes") == []

    door.refuse = False
    await digest_runner.run_digest(
        transport=FakeTransport(),
        host_factory=host_factory_for(host),
        publisher=door,
        now_utc=NOW + timedelta(days=1),
    )

    assert set(_expiries().values()) == {FIRST_PUBLISHED + timedelta(days=7)}
    (push,) = _sql("SELECT day, published_at, page_url FROM daily_pushes")
    assert push["day"] == DAY and push["published_at"] == FIRST_PUBLISHED


async def test_a_later_publication_keeps_the_first_published_at_and_restamps_new_files(env):
    subject, data_root = env
    (photo_id,), _ = await _seed(subject, data_root, photos=1, videos=0)
    first = publish.DayPublication(
        day=DAY,
        url=f"https://{PAGE_HOST}/d/x",
        online_until=FIRST_PUBLISHED + timedelta(days=7),
        published_at=FIRST_PUBLISHED,
    )
    await digest_runner.record_publication(async_session, first, retention_days=7, timezone="UTC")
    # A file of the same day uploaded after the first publication (a late derive), unstamped.
    insert_hosted_row(
        attachment_id=photo_id,
        kind="image_mail",
        dropbox_path="/memos-mail/late/p.mail.jpg",
        shared_link="https://dropbox.example/p.mail.jpg?raw=1",
        uploaded_at=FIRST_PUBLISHED + timedelta(hours=3),
    )
    later = publish.DayPublication(
        day=DAY,
        url=f"https://{PAGE_HOST}/d/x",
        online_until=FIRST_PUBLISHED + timedelta(days=8),
        published_at=FIRST_PUBLISHED + timedelta(days=1),  # a door that misbehaved
    )

    kept = await digest_runner.record_publication(
        async_session, later, retention_days=7, timezone="UTC"
    )

    assert kept == FIRST_PUBLISHED
    assert _expiries() == {(photo_id, "image_mail"): FIRST_PUBLISHED + timedelta(days=7)}


# --- AC 4: no working Dropbox in link mode defers the run ----------------------------------------


@pytest.mark.parametrize("failure", ["no_host", "upload_refused"])
async def test_a_dropbox_failure_in_link_mode_defers_mails_nothing_and_writes_no_delivery(
    env, failure
):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    (photo_id,), _ = await _seed(subject, data_root, photos=1, videos=1)
    host: FakeHost | None = None
    if failure == "upload_refused":
        host = FakeHost()
        async with async_session() as db:
            uid = await db.scalar(text(f"SELECT uid FROM attachments WHERE id = {photo_id}"))
        host.raise_on_upload = {f"{uid}.mail.jpg"}
    transport, door = FakeTransport(), StampingPublisher()

    result = await digest_runner.run_digest(
        transport=transport, host_factory=host_factory_for(host), publisher=door, now_utc=NOW
    )

    assert result is not None and result.status == digest_runner.DEFERRED
    assert transport.sent == []
    assert delivery_rows() == [] and group_rows() == []
    assert door.calls == []  # Dropbox first, page last: nothing was published
    assert run_rows()[0]["error"].startswith("link mode: ")
    # The alert: a deferred run is not a success, so the stale alert's clock does not move.
    async with async_session() as db:
        snapshot = await metrics.read_snapshot(db)
    assert snapshot.last_success == 0.0


# --- AC 5: the sweep deletes by expires_at and retires days through the outbox --------------------


def _push(day: date, published_at: datetime) -> None:
    _sql(
        "INSERT INTO daily_pushes (day, published_at, page_url) VALUES (:day, :at, :url)",
        day=day,
        at=published_at,
        url=f"https://{PAGE_HOST}/d/{day.isoformat()}",
    )


def _outbox() -> list[dict]:
    return _sql("SELECT kind, payload, acked_at FROM daily_sync_outbox ORDER BY id")


@pytest.fixture
def no_http(monkeypatch):
    """Every HTTP request any client in this process makes — the door has no other way in."""
    requests: list[str] = []

    async def refuse(self, request, *args, **kwargs):
        requests.append(str(request.url))
        raise AssertionError(f"unexpected HTTP call {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    return requests


async def test_the_sweep_deletes_by_expires_at_not_by_upload_age(env, no_http):
    now = datetime.now(UTC)
    old_but_live = insert_hosted_row(
        kind="image_mail",
        dropbox_path="/memos-mail/old/live.mail.jpg",
        shared_link="https://dropbox.example/live.mail.jpg?raw=1",
        uploaded_at=now - timedelta(days=40),
        expires_at=now + timedelta(days=2),
    )
    young_but_expired = insert_hosted_row(
        kind="poster",
        dropbox_path="/memos-mail/new/dead.poster.jpg",
        shared_link="https://dropbox.example/dead.poster.jpg?raw=1",
        uploaded_at=now - timedelta(days=1),
        expires_at=now - timedelta(minutes=1),
    )
    unstamped = insert_hosted_row(
        kind="video",
        dropbox_path="/memos-mail/old/unpublished.mp4",
        shared_link="https://dropbox.example/unpublished.mp4?raw=1",
        uploaded_at=now - timedelta(days=40),
        expires_at=None,
    )
    host = FakeHost()

    swept = await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(host))

    assert swept == 1
    assert host.deletes == ["/memos-mail/new/dead.poster.jpg"]
    status = {row["id"]: row["status"] for row in hosted_rows()}
    assert status == {old_but_live: "uploaded", young_but_expired: "swept", unstamped: "uploaded"}
    assert no_http == []


async def test_the_sweep_enqueues_exactly_one_day_delete_per_retired_day(env, no_http):
    now = datetime.now(UTC)
    _push(date(2026, 6, 1), now - timedelta(days=9))
    _push(date(2026, 6, 2), now - timedelta(days=7, minutes=1))
    _push(date(2026, 6, 8), now - timedelta(days=2))

    await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(FakeHost()))
    await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(FakeHost()))

    assert _outbox() == [
        {"kind": "day_delete", "payload": {"day": "2026-06-01"}, "acked_at": None},
        {"kind": "day_delete", "payload": {"day": "2026-06-02"}, "acked_at": None},
    ]
    assert no_http == []  # enqueued, never called: the outbox drain owns the door (MEMNG-1247)


async def test_a_day_is_retired_even_when_dropbox_is_unavailable(env, no_http):
    _push(date(2026, 6, 1), datetime.now(UTC) - timedelta(days=9))

    await digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(None))

    assert [row["payload"] for row in _outbox()] == [{"day": "2026-06-01"}]


# --- pre-gate cases -------------------------------------------------------------------------------


async def test_publishing_a_day_stamps_only_that_days_live_files(env):
    """Scope: the stamp reaches the published day's files and no other day's, and never a swept
    row. The day is the memo's LOCAL day — 23:30 UTC on the 29th is the 30th in Zurich."""
    subject, data_root = env
    (late_evening,), _ = await _seed(
        subject, data_root, photos=1, videos=0, created_at=datetime(2026, 6, 29, 23, 30, tzinfo=UTC)
    )
    (day_before,), _ = await _seed(
        subject, data_root, photos=1, videos=0, created_at=datetime(2026, 6, 29, 9, 0, tzinfo=UTC)
    )
    (swept_photo,), _ = await _seed(subject, data_root, photos=1, videos=0)
    for attachment_id, status in (
        (late_evening, "uploaded"),
        (day_before, "uploaded"),
        (swept_photo, "swept"),
    ):
        insert_hosted_row(
            attachment_id=attachment_id,
            kind="image_mail",
            status=status,
            dropbox_path=f"/memos-mail/r/{attachment_id}.mail.jpg",
            shared_link="https://dropbox.example/x?raw=1",
            uploaded_at=FIRST_PUBLISHED,
        )
    publication = publish.DayPublication(
        day=DAY,
        url=f"https://{PAGE_HOST}/d/x",
        online_until=FIRST_PUBLISHED + timedelta(days=7),
        published_at=FIRST_PUBLISHED,
    )

    await digest_runner.record_publication(
        async_session, publication, retention_days=7, timezone="Europe/Zurich"
    )

    expiries = {
        row["attachment_id"]: row["expires_at"]
        for row in _sql("SELECT attachment_id, expires_at FROM digest_hosted_media")
    }
    assert expiries == {
        late_evening: FIRST_PUBLISHED + timedelta(days=7),
        day_before: None,
        swept_photo: None,
    }


async def test_two_sweeps_racing_enqueue_one_day_delete(env):
    """Concurrency: two sweep passes at once still retire a day exactly once."""
    _push(date(2026, 6, 1), datetime.now(UTC) - timedelta(days=9))

    await asyncio.gather(
        digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(None)),
        digest_runner.sweep_hosted_media_safe(host_factory=host_factory_for(None)),
    )

    assert len(_outbox()) == 1


async def test_an_attachments_mode_upload_expires_retention_days_after_it_landed(env):
    """Attachments mode keeps its clock: no page, so its files expire from the upload."""
    subject, data_root = env
    set_digest_settings(delivery_mode="attachments", retention_days=30)
    add_recipients(*ENGLISH, language="en")
    _, (video_id,) = await _seed(subject, data_root, photos=0, videos=1)

    await digest_runner.run_digest(
        transport=FakeTransport(), host_factory=host_factory_for(FakeHost()), now_utc=NOW
    )

    (row,) = _sql("SELECT kind, uploaded_at, expires_at FROM digest_hosted_media")
    assert row["kind"] == "video"
    assert row["expires_at"] == row["uploaded_at"] + timedelta(days=30)


async def test_a_mail_picture_never_lands_on_the_full_size_copy_of_the_same_photo(env):
    """Literal in a string: the kind is spelled in the FILENAME, so the photo's full-size copy
    (`<uid>.jpg`) and its mail picture in the same run folder are two remote objects, and the
    claim's natural key (`dropbox_path`) never hands the mail upload the full-size copy's row."""
    subject, data_root = env
    (photo_id,), _ = await _seed(subject, data_root, photos=1, videos=0)
    async with async_session() as db:
        uid = await db.scalar(text(f"SELECT uid FROM attachments WHERE id = {photo_id}"))
    host = FakeHost()
    full = insert_hosted_row(
        attachment_id=photo_id,
        kind="image_full",
        dropbox_path=host.dropbox_path_for(f"{uid}.jpg"),
        shared_link="https://dropbox.example/full.jpg?raw=1",
        uploaded_at=FIRST_PUBLISHED,
    )
    async with async_session() as db:
        existing = await digest_runner.existing_hosted_links(db, [photo_id])
        settings = await digest_runner.load_settings(db)
        export = await digest_runner.load_export_config(db)
    candidates = (
        await digest_runner.selection_mod.select_for_run(
            async_session, settings, NOW, language="en"
        )
    ).included
    media = await hosting.host_link_media(
        candidates,
        host=host,
        ledger=digest_runner.SessionLedger(async_session),
        existing=existing,
        max_videos_per_memo=2,
        mail_profile=export.mail_profile,
    )

    assert media.failed == []
    rows = {row["kind"]: row for row in hosted_rows()}
    assert rows["image_full"]["id"] == full
    assert rows["image_full"]["shared_link"] == "https://dropbox.example/full.jpg?raw=1"
    assert rows["image_mail"]["dropbox_path"] != rows["image_full"]["dropbox_path"]
    assert rows["image_mail"]["status"] == "uploaded"
