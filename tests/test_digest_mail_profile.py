"""The digest's inline images ride the MAIL profile (MEMNG-240).

Everything here is asserted on the SERIALISED MIME message, through a JPEG marker parser this
file writes itself. That is deliberate and it is the whole point of the suite: Pillow encodes the
image, so asking Pillow what it encoded is a conversation the encoder has with itself — it would
stay green if `subsampling=` or `progressive=` were dropped from the call and Pillow's own
defaults answered instead. `_jpeg_facts` reads the bytes the SMTP server would receive, segment by
segment, and knows nothing about how they were made. Its own positive control
(`test_the_jpeg_oracle_can_actually_tell_the_profiles_apart`) is what makes a green run mean
something.

The other load-bearing property is that a run reads the profile ONCE. It is proved twice over: by
counting the reads, and by CHANGING the settings row in the database between two parts of one real
run and requiring the second part's images to still be the first part's profile.
"""

import random
import secrets
from datetime import UTC, datetime
from io import BytesIO

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import MailSubsampling, User, Visibility
from app.services import export_settings, media
from app.services.digest import render as digest_render
from app.services.digest import runner as digest_runner
from app.services.digest import splitting as digest_splitting
from PIL import Image
from sqlalchemy import delete

from tests.digest_support import (
    FakeTransport,
    add_recipients,
    inline_jpegs,
    jpeg_bytes,
    jpeg_facts,
    mail_profile,
    reset_digest_state,
    reset_export_settings,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    set_export_settings,
    write_mobile,
)

RECIPIENTS = ["a@example.com", "b@example.com"]
SENDER = "sender@example.com"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)

# The two profiles the tests contrast. Both are admissible settings values (the router's Literal
# allows 1200/1600/2048 and q77-83), so neither is a shape production could not produce.
DEFAULT_LONG_EDGE = 1600
NARROW_LONG_EDGE = 1200


# --- the oracle's own controls --------------------------------------------------------------------
#
# `jpeg_facts` lives in tests/digest_support.py because two suites read the same bytes with it.
# Its controls live HERE, with the assertions that depend on it.


def test_the_jpeg_oracle_can_actually_tell_the_profiles_apart():
    """The positive control on the parser, without which every assertion below is unfalsifiable.

    Two encodings that differ in exactly the fields the suite asserts on must READ as different.
    A parser that returned a constant, or one that silently fell through to a default, fails here.
    """
    source = jpeg_bytes((900, 600))
    baseline = media.encode_for_mail(source, mail_profile())
    progressive_444 = media.encode_for_mail(
        source,
        mail_profile(
            progressive=True,
            subsampling=MailSubsampling.C444,
            long_edge=NARROW_LONG_EDGE,
        ),
    )
    assert baseline is not None and progressive_444 is not None
    first, second = jpeg_facts(baseline.data), jpeg_facts(progressive_444.data)
    assert (first.progressive, first.subsampling) == (False, MailSubsampling.C420)
    assert (second.progressive, second.subsampling) == (True, MailSubsampling.C444)
    assert first.has_icc and second.has_icc  # both are tagged; the flag is not a constant either


def test_the_jpeg_oracle_refuses_something_that_is_not_a_jpeg():
    with pytest.raises(AssertionError):
        jpeg_facts(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)


# --- the encoder ---------------------------------------------------------------------------------


def test_the_long_edge_is_a_cap_and_never_an_upscale():
    """`long_edge` bounds the picture; it does not define it. A tier already under the cap keeps
    its own size — inventing pixels would cost bytes for detail that is not there."""
    big = media.encode_for_mail(jpeg_bytes((3000, 1500)), mail_profile())
    small = media.encode_for_mail(jpeg_bytes((400, 300)), mail_profile())
    assert big is not None and small is not None
    assert (big.width, big.height) == (1600, 800)
    assert (small.width, small.height) == (400, 300)


def test_a_portrait_photo_is_capped_on_its_LONG_edge():
    tall = media.encode_for_mail(jpeg_bytes((1000, 4000)), mail_profile())
    assert tall is not None
    assert (tall.width, tall.height) == (400, 1600)


def test_a_source_that_will_not_decode_costs_one_photo_and_not_the_run():
    """None, never an exception: a truncated or half-written tier degrades ONE card — the same
    degradation a missing file already causes — and the mail goes out."""
    assert media.encode_for_mail(b"not an image at all", mail_profile()) is None


def test_a_subsampling_this_deployment_cannot_encode_is_refused_loudly():
    """The other direction. An unknown value can only come from a hand-edited settings row, and
    quietly substituting a default would ship bytes nobody asked for."""
    with pytest.raises(ValueError, match="unknown mail subsampling"):
        media.encode_for_mail(jpeg_bytes(), mail_profile(subsampling="4:1:1"))


def test_the_encoded_size_travels_with_the_bytes():
    """The template states the height, so the encoder has to report what it produced — the
    profile's long edge is a cap and says nothing about the picture that came out of it."""
    encoded = media.encode_for_mail(jpeg_bytes((800, 450)), mail_profile())
    assert encoded is not None
    assert jpeg_facts(encoded.data).width == encoded.width
    assert jpeg_facts(encoded.data).height == encoded.height


def _high_entropy_jpeg(size=(512, 512)) -> bytes:
    """A tier whose pixels survive as noise: 4:4:4 at q95, so the chroma entropy is still there
    when the mail encoder decodes it. `jpeg_bytes(noise=True)` writes at Pillow's default
    subsampling, which halves the chroma and makes the re-encode fit the buffer this test is
    about."""
    width, height = size
    rng = random.Random(20260909)
    pixels = bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    image = Image.frombytes("RGB", size, pixels)
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=95, subsampling=0)
    return buffer.getvalue()


def test_a_high_entropy_tier_still_encodes_inline():
    """The mail rendition takes the same optimized save as the print one (MEMNG-241): a scan that
    does not fit the write buffer libjpeg is handed aborts the encode, and here that shows up as a
    dropped photo rather than a raised error — the card would go out without its picture."""
    encoded = media.encode_for_mail(
        _high_entropy_jpeg(), mail_profile(subsampling=MailSubsampling.C444)
    )

    assert encoded is not None
    assert (encoded.width, encoded.height) == (512, 512)
    facts = jpeg_facts(encoded.data)
    assert (facts.width, facts.height) == (512, 512)
    assert facts.subsampling == MailSubsampling.C444
    # More scan than the pixel count Pillow sizes the buffer from: the overflowing encode really
    # is the one that ran.
    assert len(encoded.data) > 512 * 512


# --- the display height ---------------------------------------------------------------------------


def test_the_display_height_keeps_the_aspect_ratio_at_the_layout_width():
    assert digest_render.display_height(1600, 1200, 540) == 405
    assert digest_render.display_height(1600, 1200, 262) == 197
    assert digest_render.display_height(1000, 3000, 540) == 1620  # portrait is taller, not clipped


def test_the_display_height_is_omitted_rather_than_guessed():
    assert digest_render.display_height(None, None, 540) is None
    assert digest_render.display_height(0, 100, 540) is None
    assert digest_render.display_height(100, 0, 540) is None


def test_a_degenerate_ratio_never_becomes_a_zero_height():
    """A 1 px tall banner against a 540 px lane rounds to 0, and `height="0"` hides the image in
    the clients that honour the attribute. One pixel is the floor."""
    assert digest_render.display_height(4000, 1, 540) == 1


# --- a whole run --------------------------------------------------------------------------------


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    """A configured, armed digest with two recipients and clean digest AND export settings."""
    reset_digest_state()
    reset_export_settings()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    add_recipients(*RECIPIENTS)
    set_digest_settings(enabled=True, start_date="2026-06-01", grace_minutes=30)
    subject = f"digest-mail-profile-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        reset_export_settings()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


def _one_card_cap(data_root, *, content: str = "memo number 0") -> int:
    """A `max_email_bytes` that fits exactly ONE of these cards and never two.

    Computed from the bytes the mail profile actually produces rather than written down: a
    hardcoded cap is a guess about an encoder, and it would start deferring cards (or stop
    splitting at all) the first time a quality default moved.

    The card's BODY is in the number too, because the splitter counts it (`card_body_bytes`): a
    cap sized from the image alone is a few dozen bytes short of the card it claims to fit, and
    the card is then deferred instead of sent — which is the same "fits exactly one" contract,
    just measured against everything the message actually carries.
    """
    sizes = []
    for tier in sorted((data_root / "assets" / "mobile").iterdir()):
        encoded = media.encode_for_mail(tier.read_bytes(), mail_profile())
        assert encoded is not None
        sizes.append(len(encoded.data))
    body = digest_splitting.card_body_bytes(
        digest_render.Card(
            uid="cap",
            memo_id=0,
            content_html=digest_render.sanitize_markdown(content, frozenset()),
            created_at=NOW,
        )
    )
    # The LARGEST, so every card fits alone rather than only the one that happened to be first:
    # each fixture carries its own random pixels and they do not encode to the same size. Two
    # cards still cannot share a part at this cap, whatever their sizes — the per-attachment
    # allowance alone doubles.
    return digest_splitting.estimate_mime_size(max(sizes) + body, 1)


async def _seed(subject: str, data_root, count: int, *, size=(2400, 1800), noise=False):
    """`count` public photo memos whose mobile tier is a real JPEG of `size`."""
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        for index in range(count):
            memo = await seed_memo(
                db,
                user,
                content=f"memo number {index}",
                visibility=Visibility.PUBLIC,
                created_at=datetime(2026, 6, 20, 10, index, tzinfo=UTC),
            )
            attachment = await seed_attachment(db, user, memo, mime="image/jpeg")
            write_mobile(data_root, attachment.uid, size=size, noise=noise)
        await db.commit()


@pytest.mark.parametrize(
    ("long_edge", "subsampling", "progressive"),
    [
        # The shipped defaults — what the household's mail actually looks like.
        (DEFAULT_LONG_EDGE, MailSubsampling.C420, False),
        # And a profile whose every field DIFFERS from Pillow's own defaults at this quality.
        # Without this row the test would stay green with `subsampling=` and `progressive=`
        # deleted from the encode call, because the library's defaults happen to agree with the
        # shipped profile — measured, not assumed (the mutation was run).
        (NARROW_LONG_EDGE, MailSubsampling.C444, True),
    ],
)
async def test_every_inline_image_of_a_sent_part_matches_the_mail_profile(
    env, long_edge, subsampling, progressive
):
    """AC 1, on the serialised message: capped at the profile's long edge, the profile's chroma
    subsampling, the profile's baseline-or-progressive choice, and sRGB-tagged."""
    subject, data_root = env
    await _seed(subject, data_root, 2)
    set_export_settings(
        mail_long_edge=long_edge,
        mail_subsampling=subsampling,
        mail_progressive=progressive,
    )
    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert len(transport.sent) == 1
    facts = inline_jpegs(transport.sent[0].message)
    assert len(facts) == 2  # control: the assertions below are over something
    for image in facts:
        assert image.long_edge == long_edge
        assert image.progressive is progressive
        assert image.subsampling == subsampling
        assert image.has_icc is True


async def test_the_html_states_each_image_width_and_height(env):
    """AC 1's other half. Without a height the card reflows when the pictures land, and in a
    client that blocks remote content it stays a sliver for ever."""
    subject, data_root = env
    await _seed(subject, data_root, 1, size=(2400, 1800))
    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)

    from tests.digest_support import html_part

    html = html_part(transport.sent[0].message)
    # 2400x1800 capped to 1600x1200, laid out in the single-photo lane at 540 px.
    assert 'width="540"' in html
    assert 'height="405"' in html


async def test_changing_the_mail_profile_changes_what_goes_out(env):
    """The profile is READ, not compiled in: the same photos, encoded differently, because the
    settings row said so."""
    subject, data_root = env
    await _seed(subject, data_root, 1)

    first = FakeTransport()
    await digest_runner.run_digest(transport=first, now_utc=NOW)
    before = inline_jpegs(first.sent[0].message)[0]

    reset_digest_state()
    add_recipients(*RECIPIENTS)
    set_digest_settings(enabled=True, start_date="2026-06-01", grace_minutes=30)
    set_export_settings(
        mail_long_edge=NARROW_LONG_EDGE,
        mail_subsampling=MailSubsampling.C444,
        mail_progressive=True,
    )
    second = FakeTransport()
    await digest_runner.run_digest(transport=second, now_utc=NOW)
    after = inline_jpegs(second.sent[0].message)[0]

    assert (before.long_edge, before.subsampling, before.progressive) == (
        DEFAULT_LONG_EDGE,
        MailSubsampling.C420,
        False,
    )
    assert (after.long_edge, after.subsampling, after.progressive) == (
        NARROW_LONG_EDGE,
        MailSubsampling.C444,
        True,
    )


async def test_a_settings_change_between_two_parts_does_not_reach_the_second(env):
    """The snapshot, proved against a real committed UPDATE rather than a stub.

    A run that resolved the profile per image — or per part — would mail the first part at 1600
    and the second at 1200, and the household would get one message whose pictures do not match
    the next. The write happens on the SMTP seam, which is the only moment a run is provably
    part-way through itself.
    """
    subject, data_root = env
    await _seed(subject, data_root, 2, size=(2400, 1800), noise=True)
    set_digest_settings(max_email_bytes=_one_card_cap(data_root))

    def change_the_profile(part: int) -> None:
        if part == 1:
            set_export_settings(mail_long_edge=NARROW_LONG_EDGE)

    transport = FakeTransport(on_sent=change_the_profile)
    await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert len(transport.sent) == 2  # control: there really was a second part to get this wrong
    for mail in transport.sent:
        for image in inline_jpegs(mail.message):
            assert image.long_edge == DEFAULT_LONG_EDGE
    # And the write really did land — otherwise the assertion above proves nothing.
    async with async_session() as db:
        config = await digest_runner.load_export_config(db)
    assert config.mail_profile.long_edge == NARROW_LONG_EDGE


async def test_the_run_reads_the_export_profiles_exactly_once(env, monkeypatch):
    """The same invariant counted rather than inferred, so a second read is caught even when it
    would have returned the same answer.

    Counted at the ACCESSOR, not at the runner's wrapper. `export_settings.settings_row` is the
    one place the singleton is ever fetched — `load_config`, `print_profile` and
    `resolve_print_profile` all go through it — so this sees a read from anywhere in the process,
    including one a future encoder made behind the renderer's back. Wrapping
    `runner.load_export_config` instead would have counted only the read that is already correct.
    """
    subject, data_root = env
    await _seed(subject, data_root, 2, size=(1200, 900), noise=True)
    set_digest_settings(max_email_bytes=_one_card_cap(data_root))

    real = export_settings.settings_row
    calls: list[int] = []

    async def counting(db, **kwargs):
        calls.append(1)
        return await real(db, **kwargs)

    monkeypatch.setattr(export_settings, "settings_row", counting)
    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert len(transport.sent) == 2  # control: two parts, four images, still one read
    assert sum(1 for mail in transport.sent for _ in inline_jpegs(mail.message)) == 2
    assert len(calls) == 1  # two parts, two encodes, one read of the settings row


async def test_the_mail_profile_costs_the_digest_fewer_parts_than_the_raw_tier(env):
    """The regression fixture that pins the improvement (AC 3).

    Measured against the SAME splitter with the SAME cap and the same photos, differing only in
    whether the message carries the mobile tier as it sits on disk (what the digest did before
    MEMNG-240) or the mail profile's encoding of it. The photos are incompressible noise, which is
    the honest case: a flat fixture compresses to nothing at any quality and would make the
    comparison meaningless.
    """
    subject, data_root = env
    await _seed(subject, data_root, 4, size=(2048, 1536), noise=True)
    cap = 18874368  # the migration's own `max_email_bytes`

    transport = FakeTransport()
    await digest_runner.run_digest(transport=transport, now_utc=NOW)
    with_profile = len(transport.sent)

    # The baseline: the very same cards, sized by the bytes of the tier files themselves.
    tier_sizes = {
        path.name: path.stat().st_size
        for path in (data_root / "assets" / "mobile").iterdir()
    }
    assert len(tier_sizes) == 4
    day_groups = digest_render.group_by_local_day(
        [
            digest_render.Card(
                uid=name,
                memo_id=index,
                content_html="",
                created_at=datetime(2026, 6, 20, 10, index, tzinfo=UTC),
                images=[digest_render.CardImage(cid=name, alt=name)],
            )
            for index, name in enumerate(sorted(tier_sizes))
        ],
        "Europe/Zurich",
    )
    baseline = len(
        digest_splitting.split_into_parts(day_groups, tier_sizes, cap).parts
    )

    assert baseline > 1  # control: the raw tier really did not fit one message
    assert with_profile < baseline
    assert with_profile == 1
