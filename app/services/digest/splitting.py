"""Greedy split of a digest into parts under `max_email_bytes`, ported from `app/splitting.py`.

The satellite's estimator, its day-then-card greedy walk and — critically — its `card_cids`
accounting are kept: a part's attachment set must include the video POSTER cids as well as the
photo cids, because the template emits posters as `cid:` images too, and omitting them is what
left dangling `cid:` refs (broken thumbnails) in a real send.

One behaviour deliberately diverges. The satellite emits an oversized card WHOLE — when no part
can hold it, `splitting.py:81-82` adds it and finalizes regardless of the cap — so one memo whose
inline images exceed `max_email_bytes` produces an over-cap message that lowering the cap can
never rescue (more parts, same oversized card). This port degrades in a fixed order instead:

1. shed PHOTO cids past the card's first photo — a shed photo becomes an "Open full size" link
   when its full-size copy is hosted, and otherwise leaves a note on the card;
2. never shed a video POSTER. Poster + play badge + Dropbox link is the binding video contract
   (user decision 4); a video whose poster was shed renders as a bare link, which is not the
   presentation the household has been receiving. `card_cids` returns photo and poster cids
   together, which is exactly why the shedding walks the two lists separately rather than that
   one;
3. if the card still exceeds the cap with every sheddable photo gone, DEFER it with a stated
   reason, visible in the preview's `deferred` list;
4. never emit an over-cap part.
"""

from dataclasses import dataclass, field

from app.services.digest.render import Card, DayGroup, InlineImage, ShedImage

# MIME base64 overhead plus per-part headers, as the satellite measured it against real messages.
MIME_OVERHEAD_FACTOR = 1.37
PER_ATTACHMENT_BYTES = 2048

# What a tagged card's chip row and plain-text tag line cost, MEASURED off the real render (the
# rendered-byte difference one chip makes, minus the tag name itself) rather than counted off the
# template by eye: the row wrapper `<div>`, one styled `<span>` per chip (email.html.j2), and the
# `#` + separator each tag adds to `render_plain`'s tag line. `card_body_bytes` adds the names
# themselves once per alternative on top.
# `tests/test_digest_splitting.py::test_the_chip_and_tag_line_allowances_are_what_the_template_
# actually_renders` re-derives all three against the live template, so a markup edit cannot leave
# them stale.
CHIP_ROW_MARKUP_BYTES = 61
PER_CHIP_MARKUP_BYTES = 185
PER_PLAIN_TAG_BYTES = 2

DEFERRED_TOO_LARGE = "card exceeds max_email_bytes after shedding"


def estimate_mime_size(raw_bytes: int, num_attachments: int = 0) -> int:
    return int(raw_bytes * MIME_OVERHEAD_FACTOR) + num_attachments * PER_ATTACHMENT_BYTES


def build_cid_to_bytes(inline_images: list[InlineImage]) -> dict[str, int]:
    return {image.cid: len(image.data) for image in inline_images}


def card_cids(card: Card) -> list[str]:
    """Every inline-image cid a card references: photos AND video poster frames."""
    return card.photo_cids() + card.poster_cids()


def card_body_bytes(card: Card) -> int:
    """The bytes a card's own BODY puts in the message: its rendered HTML, its chip row and its
    line in the text alternative.

    Left out, this is how a digest carrying no pictures estimated at ZERO however much text it
    held — the cap was then compared against nothing and one over-cap part went out. The tag row
    made the omission bigger, which is what surfaced it.

    Everything is counted ONCE PER ALTERNATIVE, because the message carries both: the body as
    HTML and again as the text `render_plain` derives from it (markup stripped, so never longer),
    and each tag name in its chip and again in the text line. Over the cap is the one direction an
    estimator must never be wrong in, so the cheaper over-count is the right one.

    What this still does NOT cover is the message's FIXED per-part chrome — the template's header
    and footer, the day headings and the MIME boundaries, about 4.5 KB measured
    (`test_a_tag_heavy_image_free_digest_...` states the number). That gap predates the tag row and
    is unchanged by it; against the 18 MB production cap it is 0.02%, and closing it would move
    every digest's split point, so it is left as a stated limit rather than folded in here.
    """
    body = len(card.content_html.encode("utf-8"))
    if not card.tags:
        return 2 * body
    names = sum(len(tag.encode("utf-8")) for tag in card.tags)
    chips = CHIP_ROW_MARKUP_BYTES + len(card.tags) * PER_CHIP_MARKUP_BYTES + names
    tag_line = names + len(card.tags) * PER_PLAIN_TAG_BYTES
    return 2 * body + chips + tag_line


def card_raw_bytes(card: Card, cid_to_bytes: dict[str, int]) -> int:
    """Everything this card contributes to a part's raw size, before the MIME overhead factor."""
    inline = sum(cid_to_bytes.get(cid, 0) for cid in card_cids(card))
    return inline + card_body_bytes(card)


def _card_att_count(card: Card) -> int:
    return len(card_cids(card))


def _fits(raw: int, atts: int, max_size: int) -> bool:
    return estimate_mime_size(raw, atts) <= max_size


@dataclass
class Part:
    day_groups: list[DayGroup]
    inline_image_cids: set[str] = field(default_factory=set)

    def cards(self) -> list[Card]:
        return [card for day in self.day_groups for card in day.cards]


@dataclass
class SplitResult:
    parts: list[Part]
    # (memo uid, reason) for every card that could not be made to fit at all.
    deferred: list[tuple[str, str]] = field(default_factory=list)


def shed_to_fit(card: Card, cid_to_bytes: dict[str, int], max_size: int) -> bool:
    """Shed photos past the first until the card alone fits. Returns whether it now does.

    Sheds from the LAST photo backwards, so the memo keeps the photo it leads with — which is the
    one the content is usually about. Video posters are never touched, at any rung: if a card
    cannot fit with its posters, it is deferred rather than sent without them.
    """
    while len(card.images) > 1:
        if _fits(card_raw_bytes(card, cid_to_bytes), _card_att_count(card), max_size):
            return True
        dropped = card.images.pop()
        if dropped.link_url:
            card.shed_links.append(ShedImage(alt=dropped.alt, url=dropped.link_url))
        else:
            card.overflow_count += 1
            card.media_incomplete = True
    return _fits(card_raw_bytes(card, cid_to_bytes), _card_att_count(card), max_size)


class _PartBuilder:
    def __init__(self, cid_to_bytes: dict[str, int], max_size: int) -> None:
        self._cid_to_bytes = cid_to_bytes
        self._max_size = max_size
        self.parts: list[Part] = []
        self._reset()

    def _reset(self) -> None:
        self._day_order: list[str] = []
        self._day_map: dict[str, DayGroup] = {}
        self.current_cids: set[str] = set()
        self.current_raw = 0
        self.current_atts = 0

    def can_fit(self, add_raw: int, add_atts: int) -> bool:
        return _fits(self.current_raw + add_raw, self.current_atts + add_atts, self._max_size)

    def _ensure_day(self, day: DayGroup) -> DayGroup:
        if day.iso not in self._day_map:
            self._day_order.append(day.iso)
            self._day_map[day.iso] = DayGroup(date_label=day.date_label, iso=day.iso, cards=[])
        return self._day_map[day.iso]

    def add_card(self, day: DayGroup, card: Card) -> None:
        self._ensure_day(day).cards.append(card)
        self.current_cids.update(card_cids(card))
        self.current_raw += card_raw_bytes(card, self._cid_to_bytes)
        self.current_atts += _card_att_count(card)

    def add_day(self, day: DayGroup) -> None:
        for card in day.cards:
            self.add_card(day, card)

    def finalize(self) -> None:
        if not self._day_map:
            return
        self.parts.append(
            Part(
                day_groups=[self._day_map[iso] for iso in self._day_order],
                inline_image_cids=set(self.current_cids),
            )
        )
        self._reset()


def split_into_parts(
    day_groups: list[DayGroup], cid_to_bytes: dict[str, int], max_email_bytes: int
) -> SplitResult:
    """Split into parts, shedding or deferring any card that cannot fit one on its own.

    The whole-digest fast path is kept: when everything fits in one message there is no split at
    all, and the single part carries every cid — which is the shape of nearly every real run.
    """
    total_raw = 0
    total_atts = 0
    for day in day_groups:
        for card in day.cards:
            total_raw += card_raw_bytes(card, cid_to_bytes)
            total_atts += _card_att_count(card)
    if _fits(total_raw, total_atts, max_email_bytes):
        cids = {cid for day in day_groups for card in day.cards for cid in card_cids(card)}
        return SplitResult(parts=[Part(day_groups=day_groups, inline_image_cids=cids)])

    builder = _PartBuilder(cid_to_bytes, max_email_bytes)
    deferred: list[tuple[str, str]] = []

    for day in day_groups:
        day_raw = sum(card_raw_bytes(c, cid_to_bytes) for c in day.cards)
        day_atts = sum(_card_att_count(c) for c in day.cards)
        if builder.can_fit(day_raw, day_atts):
            builder.add_day(day)
            continue
        if builder.current_raw > 0:
            builder.finalize()
            if builder.can_fit(day_raw, day_atts):
                builder.add_day(day)
                continue
        for card in day.cards:
            card_raw = card_raw_bytes(card, cid_to_bytes)
            card_atts = _card_att_count(card)
            if builder.can_fit(card_raw, card_atts):
                builder.add_card(day, card)
                continue
            if builder.current_raw > 0:
                builder.finalize()
                if builder.can_fit(card_raw, card_atts):
                    builder.add_card(day, card)
                    continue
            # The card does not fit an EMPTY part: this is the satellite's over-cap emission, and
            # where this port degrades instead.
            if shed_to_fit(card, cid_to_bytes, max_email_bytes):
                builder.add_card(day, card)
                continue
            deferred.append((card.uid, DEFERRED_TOO_LARGE))

    builder.finalize()
    return SplitResult(parts=builder.parts, deferred=deferred)
