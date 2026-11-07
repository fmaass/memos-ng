"""Link mode: the small mail that points at the day's page on the door (MEMNG-1246, D5).

These tests COMMIT, like the runner's own. Two systems are faked and only those: SMTP
(`FakeTransport`) and the door (`FakePublisher`, the `DayPublisher` seam MEMNG-1247 fills). The
selection, the grouping, the preview encoder, the template, the MIME builder and the delivery
ledger are the real ones.
"""

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestDeliveryMode, User, Visibility
from app.services.digest import link_mail, publish
from app.services.digest import runner as digest_runner
from sqlalchemy import delete

from tests.digest_support import (
    FakeHost,
    FakeTransport,
    add_recipients,
    group_rows,
    host_factory_for,
    html_part,
    jpeg_bytes,
    plain_part,
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
GERMAN = ["anna@example.com"]
PAGE_HOST = "dami.discomarder.live"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 Europe/Zurich


@dataclass
class FakePublisher:
    """The door, and nothing else: it records which days a run asked it to publish and answers
    one page per day on the configured page host."""

    host: str = PAGE_HOST
    calls: list[list[date]] = field(default_factory=list)

    async def publish(self, days: Sequence[date]) -> list[publish.DayPublication]:
        self.calls.append(list(days))
        return [
            publish.DayPublication(
                day=day,
                url=f"https://{self.host}/d/{day.isoformat()}-{secrets.token_hex(4)}",
                online_until=datetime(day.year, day.month, day.day, 6, tzinfo=UTC)
                + timedelta(days=7),
                published_at=datetime(day.year, day.month, day.day, 6, tzinfo=UTC),
            )
            for day in days
        ]


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(app_settings, "DAILY_PAGE_HOST", PAGE_HOST)
    set_digest_settings(
        enabled=True, start_date="2026-06-01", grace_minutes=30, delivery_mode="link"
    )
    subject = f"digest-link-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed(subject, data_root, specs) -> list[int]:
    """`specs`: (content, created_at, photo count, video count) per memo. Photos are big noise
    JPEGs, the honest shape for a byte budget; videos have an mp4 and a poster on disk."""
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        memo_ids = []
        for content, created_at, photos, videos in specs:
            memo = await seed_memo(
                db, user, content=content, visibility=Visibility.PUBLIC, created_at=created_at
            )
            for _ in range(photos):
                attachment = await seed_attachment(db, user, memo, mime="image/jpeg")
                write_mobile(data_root, attachment.uid, size=(2048, 1536), noise=True)
            for _ in range(videos):
                attachment = await seed_attachment(
                    db, user, memo, mime="video/mp4", filename="clip.mp4"
                )
                write_web_mp4(data_root, attachment.uid)
                write_poster(data_root, attachment.uid, noise=True)
            memo_ids.append(memo.id)
        await db.commit()
    return memo_ids


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html)


def _image_parts(message) -> list:
    return [part for part in message.walk() if part.get_content_maintype() == "image"]


# --- AC 1: the shape of the mail -----------------------------------------------------------------


async def test_a_link_mail_has_one_small_image_one_page_link_and_the_url_in_its_text(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed(
        subject, data_root, [("A day out", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 3, 1)]
    )
    transport, publisher = FakeTransport(), FakePublisher()

    result = await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=publisher,
        now_utc=NOW,
    )

    assert result is not None and result.status == digest_runner.SENT
    assert len(transport.sent) == 1
    message = transport.sent[0].message
    images = _image_parts(message)
    assert len(images) == 1
    assert len(images[0].get_payload(decode=True)) < link_mail.PREVIEW_MAX_BYTES
    content_types = [part.get_content_type() for part in message.walk()]
    assert not [kind for kind in content_types if kind.startswith("video/")]
    assert not any("mp4" in str(part.get_filename() or "") for part in message.walk())
    html = html_part(message)
    page_links = [href for href in _hrefs(html) if PAGE_HOST in href]
    assert len(page_links) == 1
    url = page_links[0]
    assert url.startswith(f"https://{PAGE_HOST}/d/2026-06-30-")
    assert url in plain_part(message)
    assert f"cid:{images[0]['Content-ID'].strip('<>')}" in html_part(message)
    assert {row["memo_id"] for row in group_rows()} == set(memo_ids)


async def test_the_mail_says_until_when_the_page_is_online_and_counts_the_day(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    await _seed(subject, data_root, [("A day out", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 3, 1)])
    transport = FakeTransport()

    await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=FakePublisher(),
        now_utc=NOW,
    )

    for body in (html_part(transport.sent[0].message), plain_part(transport.sent[0].message)):
        assert "Online until July 7, 2026" in body
        assert "1 memo · 3 photos · 1 video" in body
        assert "A day out" in body
        assert "older day" not in body


# --- AC 2: the target day ------------------------------------------------------------------------


async def test_a_three_day_run_links_the_newest_day_counts_only_that_day_and_names_two_older(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed(
        subject,
        data_root,
        [
            ("oldest day memo", datetime(2026, 6, 27, 9, 0, tzinfo=UTC), 2, 0),
            ("middle day memo", datetime(2026, 6, 28, 9, 0, tzinfo=UTC), 1, 1),
            ("middle day again", datetime(2026, 6, 28, 12, 0, tzinfo=UTC), 1, 0),
            ("newest day first", datetime(2026, 6, 29, 8, 0, tzinfo=UTC), 2, 0),
            ("newest day second", datetime(2026, 6, 29, 9, 0, tzinfo=UTC), 0, 0),
        ],
    )
    transport, publisher = FakeTransport(), FakePublisher()

    await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=publisher,
        now_utc=NOW,
    )

    assert publisher.calls == [[date(2026, 6, 27), date(2026, 6, 28), date(2026, 6, 29)]]
    message = transport.sent[0].message
    links = [href for href in _hrefs(html_part(message)) if PAGE_HOST in href]
    assert len(links) == 1 and "/d/2026-06-29-" in links[0]
    for body in (html_part(message), plain_part(message)):
        assert "2 memos · 2 photos" in body
        assert "video" not in body
        assert "and 2 older days" in body
        assert "newest day first" in body
        assert "middle day" not in body and "oldest day" not in body
        assert "Monday, June 29, 2026" in body
    # Every memo of the run is delivered: the older days are walked from the page, not re-sent.
    assert {row["memo_id"] for row in group_rows()} == set(memo_ids)


async def test_a_run_touching_more_than_seven_dates_publishes_all_and_links_the_newest(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    specs = [
        (f"memo of day {n}", datetime(2026, 6, 20 + n, 9, 0, tzinfo=UTC), 0, 0) for n in range(9)
    ]
    await _seed(subject, data_root, specs)
    transport, publisher = FakeTransport(), FakePublisher()

    await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=publisher,
        now_utc=NOW,
    )

    assert publisher.calls == [[date(2026, 6, 20 + n) for n in range(9)]]
    message = transport.sent[0].message
    assert [h for h in _hrefs(html_part(message)) if "/d/2026-06-28-" in h]
    assert "and 8 older days" in plain_part(message)


# --- AC 4: the host strip ---------------------------------------------------------------------


async def test_a_memos_ng_link_is_still_stripped_in_link_mode_and_the_page_host_is_not(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    content = (
        "See [home](https://memos.discomarder.live/m/abc) and "
        f"[yesterday](https://{PAGE_HOST}/d/older) and ![x](https://memos-ng.discomarder.live/x.jpg)"
    )
    await _seed(subject, data_root, [(content, datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 0, 0)])
    transport = FakeTransport()

    await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=FakePublisher(),
        now_utc=NOW,
    )

    html = html_part(transport.sent[0].message)
    assert "memos.discomarder.live" not in html
    assert "memos-ng.discomarder.live" not in html
    assert f'href="https://{PAGE_HOST}/d/older"' in html
    assert "home" in html  # the words stay; only the link to memos-ng goes


# --- the seam: no door, no mail ------------------------------------------------------------------


async def test_link_mode_without_a_configured_publisher_defers_and_sends_nothing(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    await _seed(subject, data_root, [("x", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 1, 0)])
    transport = FakeTransport()

    result = await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()), transport=transport, now_utc=NOW
    )

    assert result is not None and result.status == digest_runner.DEFERRED
    assert transport.sent == []
    assert group_rows() == []
    row = run_rows()[0]
    assert row["status"] == digest_runner.DEFERRED
    assert row["error"] == publish.ERROR_PUBLISHER_UNCONFIGURED


async def test_a_publication_off_the_page_host_is_refused_and_nothing_is_sent(env):
    subject, data_root = env
    add_recipients(*ENGLISH, language="en")
    await _seed(subject, data_root, [("x", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 1, 0)])
    transport = FakeTransport()

    result = await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=FakePublisher(host="memos.discomarder.live"),
        now_utc=NOW,
    )

    assert result is not None and result.status == digest_runner.DEFERRED
    assert transport.sent == [] and group_rows() == []


async def test_a_page_host_that_is_a_memos_ng_host_defers_the_run(env, monkeypatch):
    subject, data_root = env
    monkeypatch.setattr(app_settings, "DAILY_PAGE_HOST", "memos.discomarder.live")
    add_recipients(*ENGLISH, language="en")
    await _seed(subject, data_root, [("x", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 1, 0)])
    transport = FakeTransport()
    publisher = FakePublisher(host="memos.discomarder.live")

    result = await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=publisher,
        now_utc=NOW,
    )

    assert result is not None and result.status == digest_runner.DEFERRED
    assert transport.sent == [] and publisher.calls == []


async def test_an_empty_link_mode_run_publishes_nothing(env):
    add_recipients(*ENGLISH, language="en")
    transport, publisher = FakeTransport(), FakePublisher()

    result = await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=publisher,
        now_utc=NOW,
    )

    assert result is not None and result.status == digest_runner.NOTHING_TO_SEND
    assert publisher.calls == [] and transport.sent == []


# --- per language group (MEMNG-1245 stays intact) ------------------------------------------------


async def test_each_language_group_gets_its_own_localized_link_mail(env):
    subject, data_root = env
    add_recipients(*GERMAN, language="de")
    add_recipients(*ENGLISH, language="en")
    memo_ids = await _seed(
        subject,
        data_root,
        [
            ("eins", datetime(2026, 6, 28, 9, 0, tzinfo=UTC), 1, 0),
            ("zwei", datetime(2026, 6, 29, 9, 0, tzinfo=UTC), 1, 0),
        ],
    )
    transport, publisher = FakeTransport(), FakePublisher()

    result = await digest_runner.run_digest(
        host_factory=host_factory_for(FakeHost()),
        transport=transport,
        publisher=publisher,
        now_utc=NOW,
    )

    assert result is not None and result.status == digest_runner.SENT
    assert [mail.recipients for mail in transport.sent] == [GERMAN, ENGLISH]
    german, english = (plain_part(mail.message) for mail in transport.sent)
    assert "und 1 älterer Tag" in german and "Online bis 6. Juli 2026" in german
    assert "and 1 older day" in english and "Online until July 6, 2026" in english
    # One publish for the run, whatever the number of groups.
    assert len(publisher.calls) == 1
    assert {(row["memo_id"], row["language"]) for row in group_rows()} == {
        (m, lang) for m in memo_ids for lang in ("de", "en")
    }


# --- the probes ----------------------------------------------------------------------------------


async def test_the_preview_renders_the_link_mail_against_the_page_host_and_publishes_nothing(env):
    subject, data_root = env
    await _seed(subject, data_root, [("preview me", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 1, 0)])

    rendered, settings = await digest_runner.preview(now_utc=NOW, language="en")

    assert settings.delivery_mode == DigestDeliveryMode.link
    assert rendered.link is not None
    _, html, plain, images, _, _ = digest_runner.part_payload(rendered, 1, settings)
    assert len(images) == 1
    assert f'href="https://{PAGE_HOST}/"' in html and f"https://{PAGE_HOST}/" in plain
    assert "preview me" in html


async def test_a_link_mode_test_send_mails_the_link_mail_to_the_admin_only(env):
    subject, data_root = env
    await _seed(subject, data_root, [("probe", datetime(2026, 6, 30, 9, 0, tzinfo=UTC), 1, 1)])
    transport = FakeTransport()

    count = await digest_runner.test_send(
        address="admin@example.com", language="en", transport=transport, now_utc=NOW
    )

    assert count == 1
    assert [mail.recipients for mail in transport.sent] == [["admin@example.com"]]
    message = transport.sent[0].message
    assert len(_image_parts(message)) == 1
    assert f"https://{PAGE_HOST}/" in plain_part(message)
    assert group_rows() == []


# --- the preview image ------------------------------------------------------------------------


def test_the_preview_image_fits_under_100_kb_even_from_a_large_noisy_photo():
    image = link_mail.encode_preview(jpeg_bytes((4000, 3000), noise=True), subsampling="4:2:0")

    assert image is not None
    assert len(image.data) < link_mail.PREVIEW_MAX_BYTES
    assert max(image.width, image.height) <= link_mail.PREVIEW_LONG_EDGES[0]


def test_an_unreadable_preview_source_gives_no_image_rather_than_an_error():
    assert link_mail.encode_preview(b"not a jpeg", subsampling="4:2:0") is None
