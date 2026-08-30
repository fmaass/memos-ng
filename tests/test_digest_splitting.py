"""Greedy part-splitting, and the one deliberate divergence from the satellite (MEMNG-234).

The satellite emits an oversized card WHOLE (`splitting.py:81-82`), so one memo whose inline images
exceed `max_email_bytes` produces an over-cap message that lowering the cap can never rescue. This
port sheds photos past the first, never sheds a video poster, and defers the card if it still will
not fit. Every rung has a test, and the last assertion of the suite is the invariant the whole
divergence exists for: **no emitted part exceeds the cap.**
"""

from datetime import UTC, datetime

from app.services.digest import splitting
from app.services.digest.render import Card, CardImage, CardVideo, DayGroup, InlineImage

DAY = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _card(uid: str, photos: list[tuple[str, int]], videos: list[tuple[str, int]] | None = None,
          link_urls: dict[str, str] | None = None) -> tuple[Card, dict[str, int]]:
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
        raw = sum(sizes.get(cid, 0) for cid in part.inline_image_cids)
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
