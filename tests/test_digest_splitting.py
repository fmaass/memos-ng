"""Greedy part-splitting, and the one deliberate divergence from the satellite (MEMNG-234).

The satellite emits an oversized card WHOLE (`splitting.py:81-82`), so one memo whose inline images
exceed `max_email_bytes` produces an over-cap message that lowering the cap can never rescue. This
port sheds photos past the first, never sheds a video poster, and defers the card if it still will
not fit. Every rung has a test, and the last assertion of the suite is the invariant the whole
divergence exists for: **no emitted part exceeds the cap.**
"""

import secrets
from datetime import UTC, datetime

from app.services.digest import render, splitting
from app.services.digest.mailer import build_message
from app.services.digest.render import Card, CardImage, CardVideo, DayGroup, InlineImage
from jinja2 import Environment

DAY = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _card(
    uid: str,
    photos: list[tuple[str, int]],
    videos: list[tuple[str, int]] | None = None,
    link_urls: dict[str, str] | None = None,
) -> tuple[Card, dict[str, int]]:
    """A card plus the cid→bytes map its media contributes."""
    links = link_urls or {}
    card = Card(uid=uid, memo_id=abs(hash(uid)) % 100000, content_html="", created_at=DAY)
    sizes: dict[str, int] = {}
    for cid, size in photos:
        card.images.append(CardImage(cid=cid, alt=cid, link_url=links.get(cid)))
        sizes[cid] = size
    for cid, size in videos or []:
        card.videos.append(
            CardVideo(filename=f"{cid}.mp4", status="hosted", url="u", poster_cid=cid)
        )
        sizes[cid] = size
    return card, sizes


def _day(cards: list[Card], iso: str = "2026-07-01") -> DayGroup:
    return DayGroup(date_label="Wednesday, July 1, 2026", iso=iso, cards=cards)


def _no_part_exceeds(result: splitting.SplitResult, sizes: dict[str, int], cap: int) -> None:
    for part in result.parts:
        # The production accounting, so this reads a part exactly as the splitter decided it:
        # inline bytes AND the body bytes the cards render into.
        raw = sum(splitting.card_raw_bytes(card, sizes) for card in part.cards())
        assert splitting.estimate_mime_size(raw, len(part.inline_image_cids)) <= cap


# --- the accounting -----------------------------------------------------------------------------


def test_card_cids_counts_photos_AND_video_posters():
    """Omitting the poster cids is what left dangling `cid:` refs (broken thumbnails) in a real
    send: the template emits a poster as a `cid:` image, so it belongs to the part's attachment
    set."""
    card, _ = _card("m", photos=[("p1", 10)], videos=[("poster1", 20)])
    assert splitting.card_cids(card) == ["p1", "poster1"]
    assert card.photo_cids() == ["p1"]
    assert card.poster_cids() == ["poster1"]


def test_build_cid_to_bytes_reads_the_inline_payloads():
    images = [InlineImage(cid="a", data=b"xxx"), InlineImage(cid="b", data=b"y")]
    assert splitting.build_cid_to_bytes(images) == {"a": 3, "b": 1}


# --- the greedy walk ----------------------------------------------------------------------------


def test_everything_that_fits_stays_one_part():
    card, sizes = _card("m", photos=[("p1", 1000)])
    result = splitting.split_into_parts([_day([card])], sizes, 1_000_000)
    assert len(result.parts) == 1
    assert result.parts[0].inline_image_cids == {"p1"}
    assert result.deferred == []


def test_two_cards_that_do_not_fit_together_become_two_parts():
    a, sa = _card("a", photos=[("p1", 6000)])
    b, sb = _card("b", photos=[("p2", 6000)])
    sizes = {**sa, **sb}
    cap = splitting.estimate_mime_size(9000, 1)
    result = splitting.split_into_parts([_day([a, b])], sizes, cap)
    assert len(result.parts) == 2
    assert [sorted(p.inline_image_cids) for p in result.parts] == [["p1"], ["p2"]]
    _no_part_exceeds(result, sizes, cap)


def test_days_are_kept_together_when_they_fit():
    a, sa = _card("a", photos=[("p1", 100)])
    b, sb = _card("b", photos=[("p2", 100)])
    c, sc = _card("c", photos=[("p3", 9000)])
    sizes = {**sa, **sb, **sc}
    cap = splitting.estimate_mime_size(9500, 1)
    result = splitting.split_into_parts(
        [_day([a, b], "2026-07-02"), _day([c], "2026-07-01")], sizes, cap
    )
    assert len(result.parts) == 2
    assert sorted(result.parts[0].inline_image_cids) == ["p1", "p2"]
    _no_part_exceeds(result, sizes, cap)


# --- the shedding rungs -------------------------------------------------------------------------


def test_rung_one_sheds_photos_past_the_first():
    card, sizes = _card("m", photos=[("p1", 1000), ("p2", 9000), ("p3", 9000)])
    cap = splitting.estimate_mime_size(1500, 1)
    result = splitting.split_into_parts([_day([card])], sizes, cap)
    assert result.deferred == []
    assert [image.cid for image in card.images] == ["p1"]  # the LEAD photo is kept
    assert card.overflow_count == 2
    assert card.media_incomplete is True
    _no_part_exceeds(result, sizes, cap)


def test_a_shed_photo_becomes_an_open_full_size_link_when_it_is_hosted():
    card, sizes = _card(
        "m",
        photos=[("p1", 1000), ("p2", 9000)],
        link_urls={"p2": "https://dropbox.example/full.jpg?raw=1"},
    )
    cap = splitting.estimate_mime_size(1500, 1)
    splitting.split_into_parts([_day([card])], sizes, cap)
    assert [shed.url for shed in card.shed_links] == ["https://dropbox.example/full.jpg?raw=1"]
    # A hosted photo is not LOST, so the card does not claim media went missing.
    assert card.overflow_count == 0
    assert card.media_incomplete is False


def test_rung_two_never_sheds_a_video_poster():
    """The binding video contract is poster + play badge + link (user decision 4). A video whose
    poster was shed renders as a bare link — not the presentation the household receives."""
    card, sizes = _card("m", photos=[("p1", 500)], videos=[("v1", 9000), ("v2", 9000)])
    cap = splitting.estimate_mime_size(1000, 1)
    result = splitting.split_into_parts([_day([card])], sizes, cap)
    assert result.deferred == [("m", splitting.DEFERRED_TOO_LARGE)]
    assert card.poster_cids() == ["v1", "v2"]  # both posters intact
    assert result.parts == []


def test_a_two_video_card_keeps_every_poster_or_defers():
    card, sizes = _card("m", photos=[], videos=[("v1", 6000), ("v2", 6000)])
    cap = splitting.estimate_mime_size(5000, 2)
    result = splitting.split_into_parts([_day([card])], sizes, cap)
    emitted = [c for part in result.parts for c in part.cards()]
    if emitted:
        assert emitted[0].poster_cids() == ["v1", "v2"]
    else:
        assert result.deferred == [("m", splitting.DEFERRED_TOO_LARGE)]
    _no_part_exceeds(result, sizes, cap)


def test_a_photo_followed_by_a_video_sheds_the_photo_and_keeps_the_poster():
    """The mixed card: shedding must walk the photo list, not `card_cids`, or it would take the
    poster with it."""
    card, sizes = _card("m", photos=[("p1", 400), ("p2", 9000)], videos=[("v1", 400)])
    cap = splitting.estimate_mime_size(1000, 2)
    result = splitting.split_into_parts([_day([card])], sizes, cap)
    assert result.deferred == []
    assert card.photo_cids() == ["p1"]
    assert card.poster_cids() == ["v1"]
    _no_part_exceeds(result, sizes, cap)


def test_rung_three_defers_with_a_stated_reason():
    card, sizes = _card("huge", photos=[("p1", 10_000_000)])
    cap = 2_000_000
    result = splitting.split_into_parts([_day([card])], sizes, cap)
    assert result.deferred == [("huge", splitting.DEFERRED_TOO_LARGE)]
    assert result.parts == []


def test_a_deferred_card_does_not_hold_back_the_others():
    huge, sh = _card("huge", photos=[("big", 10_000_000)])
    small, ss = _card("small", photos=[("p1", 100)])
    sizes = {**sh, **ss}
    cap = 2_000_000
    result = splitting.split_into_parts([_day([huge, small])], sizes, cap)
    assert [uid for uid, _ in result.deferred] == ["huge"]
    emitted = [card.uid for part in result.parts for card in part.cards()]
    assert emitted == ["small"]
    _no_part_exceeds(result, sizes, cap)


def test_no_emitted_part_ever_exceeds_the_cap():
    """Rung 4, and the invariant the whole divergence exists for. The satellite fails this."""
    cards = []
    sizes: dict[str, int] = {}
    for index in range(6):
        card, card_sizes = _card(
            f"m{index}",
            photos=[(f"p{index}a", 3000), (f"p{index}b", 4000)],
            videos=[(f"v{index}", 2000)],
        )
        cards.append(card)
        sizes.update(card_sizes)
    cap = splitting.estimate_mime_size(9000, 3)
    result = splitting.split_into_parts([_day(cards)], sizes, cap)
    assert result.parts
    _no_part_exceeds(result, sizes, cap)


def test_shed_to_fit_reports_failure_rather_than_lying():
    card, sizes = _card("m", photos=[("p1", 10_000_000)])
    assert splitting.shed_to_fit(card, sizes, 1000) is False
    assert len(card.images) == 1  # the first photo is never shed


# --- an independent oracle ----------------------------------------------------------------------


def _serialised_size(part: splitting.Part, payloads: dict[str, bytes]) -> int:
    """The bytes an SMTP server would actually receive for this part.

    Deliberately built with the REAL message builder and measured with `as_bytes()`: base64
    expansion, the MIME boundaries, the per-part headers and both alternative bodies are all in
    the number, and none of them comes from the estimator under test.
    """
    message = build_message(
        subject="The Daily Dami Memos Mail",
        from_email="sender@example.com",
        from_name="The Daily Dami Memos Mail",
        html_body="<html><body><p>a card</p></body></html>",
        plain_body="a card",
        inline_images=[
            InlineImage(cid=cid, data=payloads[cid]) for cid in sorted(part.inline_image_cids)
        ],
    )
    return len(message.as_bytes())


def test_the_serialised_bytes_of_every_part_fit_under_the_cap():
    """The invariant measured by something OTHER than the thing being tested.

    Every other assertion in this file about "no part exceeds the cap" runs the part back through
    `estimate_mime_size` — the same production estimator the splitter used to decide. That check
    can only ever agree with itself: an estimator that under-counted by a factor of two would keep
    it green while every message went out over the cap. This one serialises the real MIME message
    and counts its bytes, so the estimator has to be CONSERVATIVE rather than merely
    self-consistent.

    The payloads are incompressible random bytes, which is the honest case: base64 expands them by
    4/3 with no encoder able to win any of it back.
    """
    cap = 200_000
    payloads = {f"p{index}": secrets.token_bytes(40_000) for index in range(6)}
    cards: list[Card] = []
    sizes: dict[str, int] = {}
    for index, (cid, data) in enumerate(payloads.items()):
        card, card_sizes = _card(f"m{index}", photos=[(cid, len(data))])
        cards.append(card)
        sizes.update(card_sizes)

    result = splitting.split_into_parts([_day(cards)], sizes, cap)
    # Control: a single part would prove nothing about a SPLIT, and an empty result nothing at all.
    assert len(result.parts) > 1
    assert result.deferred == []
    assert sum(len(part.inline_image_cids) for part in result.parts) == len(payloads)
    for part in result.parts:
        assert _serialised_size(part, payloads) <= cap


def test_the_independent_oracle_can_actually_fail():
    """Positive control on the oracle itself: a part the splitter would never emit — every photo
    in one message — must be measured as over the cap. Without this, a `_serialised_size` that
    returned 0 would leave the test above green for ever."""
    cap = 200_000
    payloads = {f"p{index}": secrets.token_bytes(40_000) for index in range(6)}
    oversized = splitting.Part(day_groups=[], inline_image_cids=set(payloads))
    assert _serialised_size(oversized, payloads) > cap


# --- the body a card renders is part of the message too (MEMNG-275 follow-up) --------------------
#
# `card_raw_bytes` counted inline images and nothing else, so an image-free digest estimated at
# ZERO bytes however much text it carried. The chip row and the plain-text tag line made that
# omission bigger, which is what these tests hold.

FROM_NAME = "The Daily Dami Memos Mail"


def _rendered_size(part: splitting.Part, payloads: dict[str, bytes]) -> int:
    """The bytes an SMTP server receives for this part, with its REAL rendered body.

    The other oracle in this file sends a stub body, which is honest for a test about image bytes
    and useless for one about body bytes. This one renders the part through the production
    template and text builder, so the number includes every chip and every tag line.
    """
    subject = f"{FROM_NAME} — July 1, 2026"
    cards = part.cards()
    html = render.render_html(
        part.day_groups,
        subject=subject,
        mail_title=FROM_NAME,
        date_range="July 1, 2026",
        memo_count=len(cards),
    )
    plain = render.render_plain(part.day_groups, subject=subject, memo_count=len(cards))
    message = build_message(
        subject=subject,
        from_email="sender@example.com",
        from_name=FROM_NAME,
        html_body=html,
        plain_body=plain,
        inline_images=[
            InlineImage(cid=cid, data=payloads[cid]) for cid in sorted(part.inline_image_cids)
        ],
    )
    return len(message.as_bytes())


def _tagged_card(uid: str, tags: tuple[str, ...], body: str) -> Card:
    return Card(
        uid=uid,
        memo_id=abs(hash(uid)) % 100000,
        content_html=f"<p>{body}</p>",
        created_at=DAY,
        tags=tags,
    )


def test_the_chip_and_tag_line_allowances_are_what_the_template_actually_renders():
    """The constants are DERIVED, and this is where they stay derived: the same measurement that
    produced them, run against the live template and text builder. A markup edit that changes what
    a chip costs turns this red instead of leaving the estimate quietly stale."""
    name = "aaaa"

    def sizes(count: int) -> tuple[int, int]:
        card = _tagged_card("m", tuple(name for _ in range(count)), "body")
        groups = render.group_by_local_day([card], "Europe/Zurich")
        html = render.render_html(groups, subject="s", mail_title="t", date_range="r", memo_count=1)
        plain = render.render_plain(groups, subject="s", memo_count=1)
        return len(html.encode("utf-8")), len(plain.encode("utf-8"))

    (html0, plain0), (html1, plain1), (html2, plain2) = sizes(0), sizes(1), sizes(2)
    assert html2 - html1 == splitting.PER_CHIP_MARKUP_BYTES + len(name)
    assert html1 - html0 == (
        splitting.CHIP_ROW_MARKUP_BYTES + splitting.PER_CHIP_MARKUP_BYTES + len(name)
    )
    assert plain2 - plain1 == splitting.PER_PLAIN_TAG_BYTES + len(name)
    assert plain1 - plain0 == splitting.PER_PLAIN_TAG_BYTES + len(name)


def test_a_card_with_no_media_is_no_longer_estimated_at_zero_bytes():
    card = _tagged_card("m", ("curated", "trip"), "a body")
    assert splitting.card_raw_bytes(card, {}) > len(card.content_html.encode("utf-8"))
    bare = Card(uid="m2", memo_id=2, content_html="", created_at=DAY)
    assert splitting.card_raw_bytes(bare, {}) == 0  # nothing to render, nothing to count


def test_a_tag_heavy_image_free_digest_splits_instead_of_emitting_one_over_cap_part():
    """The regression this fold exists for: 40 image-free memos carrying 40 tags each estimated at
    ZERO under the old accounting — the splitter emitted one part whose real MIME was far over the
    cap. The oracle here is the serialised message, not the estimator."""
    cap = 262_144
    cards = [
        _tagged_card(
            f"m{index}",
            tuple(f"tag-{index:02d}{slot:02d}" for slot in range(40)),
            f"memo {index} " * 20,
        )
        for index in range(40)
    ]
    sizes: dict[str, int] = {}
    # Control: no media at all, so the OLD accounting (inline bytes only) saw nothing to count.
    assert all(splitting.card_cids(card) == [] for card in cards)
    assert splitting.estimate_mime_size(0, 0) == 0
    whole = splitting.Part(day_groups=[_day(cards)], inline_image_cids=set())
    assert _rendered_size(whole, {}) > cap  # the digest really is over the cap in real bytes

    # The message's FIXED chrome — template header and footer, day heading, MIME boundaries — is
    # not in `estimate_mime_size` at all and never was (it has no per-part term). Measured here
    # with the same oracle rather than written down, so the residual this change does NOT close is
    # a number the test states instead of a rounding nobody checks.
    chrome = _rendered_size(splitting.Part(day_groups=[], inline_image_cids=set()), {})
    assert chrome > 0
    assert _rendered_size(whole, {}) > cap + chrome  # the split is needed for real, not by chrome

    result = splitting.split_into_parts([_day(cards)], sizes, cap)
    assert len(result.parts) >= 2
    assert result.deferred == []
    assert sum(len(part.cards()) for part in result.parts) == len(cards)
    for part in result.parts:
        assert _rendered_size(part, {}) <= cap + chrome


def test_an_ordinary_digest_of_seven_photo_memos_is_still_one_part():
    """The no-regression half: the shape of nearly every real run — seven memos, two photos each,
    a couple of tags — must still be a single message under the production cap."""
    cap = 18_874_368
    cards: list[Card] = []
    sizes: dict[str, int] = {}
    for index in range(7):
        card, card_sizes = _card(
            f"m{index}", photos=[(f"p{index}a", 200_000), (f"p{index}b", 200_000)]
        )
        card.content_html = "<p>a normal memo with a sentence or two in it</p>"
        card.tags = ("curated", "trip")
        cards.append(card)
        sizes.update(card_sizes)
    result = splitting.split_into_parts([_day(cards)], sizes, cap)
    assert len(result.parts) == 1
    assert result.deferred == []
    _no_part_exceeds(result, sizes, cap)


def test_the_estimator_escapes_a_tag_name_the_way_the_template_does():
    """A chip renders `{{ tag | e }}`, so an imported tag name full of ampersands costs the message
    five bytes for every one it is written with. The estimator therefore escapes with the SAME
    function the template applies — asserted by identity, not by imitation, because a hand-rolled
    `html.escape` differs from Jinja's on quotes (`&quot;` vs `&#34;`) and would drift again."""
    assert splitting.html_escape is Environment().filters["e"]
    name = "t00_" + "&" * 196
    escaped = str(splitting.html_escape(name))
    assert len(escaped) == 4 + 196 * 5  # `&` -> `&amp;`

    def html_size(count: int) -> int:
        card = _tagged_card("m", tuple(name for _ in range(count)), "body")
        groups = render.group_by_local_day([card], "Europe/Zurich")
        return len(
            render.render_html(
                groups, subject="s", mail_title="t", date_range="r", memo_count=1
            ).encode("utf-8")
        )

    # What the template really spends on one more chip of this name, against what the estimator
    # would spend: the escaped name, never the raw one.
    assert html_size(2) - html_size(1) == splitting.PER_CHIP_MARKUP_BYTES + len(
        escaped.encode("utf-8")
    )
    assert splitting.card_body_bytes(_tagged_card("m", (name,), "body")) == (
        2 * len("<p>body</p>")
        + splitting.CHIP_ROW_MARKUP_BYTES
        + splitting.PER_CHIP_MARKUP_BYTES
        + len(escaped.encode("utf-8"))
        + len(name.encode("utf-8"))
        + splitting.PER_PLAIN_TAG_BYTES
    )


def test_an_escaping_heavy_tag_set_still_splits_under_the_real_message_size():
    """The reviewer's reproduction: 40 image-free cards, 40 imported tags each, every name 200
    bytes raw and 984 escaped. Counting the raw length estimated a 32-card part at 1,033,001 bytes
    that serialises to 2,388,966 — over twice the cap. Tag names are unrestricted (they arrive from
    the memos import), so this is a real message shape, not a constructed one."""
    cap = 1_048_576
    names = tuple(f"t{slot:02d}_" + "&" * 196 for slot in range(40))
    cards = [_tagged_card(f"m{index}", names, f"memo {index}") for index in range(40)]
    chrome = _rendered_size(splitting.Part(day_groups=[], inline_image_cids=set()), {})
    whole = splitting.Part(day_groups=[_day(cards)], inline_image_cids=set())
    assert _rendered_size(whole, {}) > cap + chrome  # control: it really does not fit one message

    result = splitting.split_into_parts([_day(cards)], sizes := {}, cap)
    assert sizes == {}  # no media anywhere: the body bytes are the whole estimate
    assert len(result.parts) >= 2
    assert result.deferred == []
    assert sum(len(part.cards()) for part in result.parts) == len(cards)
    for part in result.parts:
        assert _rendered_size(part, {}) <= cap + chrome
