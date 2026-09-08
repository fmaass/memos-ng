"""Memo Markdown → the mail body, ported from the satellite's `app/render.py`.

The template is the satellite's `email.html.j2`, and the sanitiser allow-lists are its
`ALLOWED_TAGS` / `ALLOWED_ATTRS` — this exact body has been landing in these six inboxes for
months, and the household knows what the digest looks like.

One rule is added, and it is a security rule rather than a cosmetic one: **no absolute URL back to
memos-ng survives into the rendered mail.** The template's own `src` values are all `cid:` and its
own `href` values are all Dropbox, so the risk is not the template — it is a memo whose Markdown
contains a link to this instance, which the sanitiser's `a[href]` / `img[src]` allowance would
carry straight through. The recipients are outside the household and the mail must never point
them here (owner decision on design 3285), so the host set is stripped at the one place every
memo's content passes through.
"""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import bleach
import markdown
from anyio import to_thread
from jinja2 import Environment, FileSystemLoader, Template, select_autoescape

from app.config import settings as app_settings
from app.services import media
from app.services.asset_paths import derived_tier_path
from app.services.digest.gate import FALLBACK_TIMEZONE, resolve_zone
from app.services.digest.hosting import MediaLinks
from app.services.digest.select import MAIL_IMAGE_TIER, Candidate
from app.services.export_settings import MailProfile
from app.services.tag_lines import strip_tag_only_lines

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "email.html.j2"

# The CSS width the template lays each inline image out at. They are here rather than only in the
# template because the `height` attribute is computed from them, and a width that lived in two
# places would put a distorted picture in the mail the first time one of them moved.
SINGLE_IMAGE_WIDTH = 540  # a card with one photo, and every video poster
GRID_IMAGE_WIDTH = 262  # a card with two or more, in the two-column grid

# The satellite's allow-lists, unchanged (`app/render.py:22-39`).
ALLOWED_TAGS = [
    "a", "abbr", "b", "blockquote", "br", "code", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "strong",
    "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
]
ALLOWED_ATTRS: dict[str, list[str]] = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
    "td": ["align"],
    "th": ["align"],
}

# Video card states, as the template branches on them.
HOSTED = "hosted"
PROCESSING = "processing"
NOT_HOSTED = "no_uploader"

# An `<img>` bleach left with no `src`, because the URL pointed at memos-ng and the attribute was
# refused. bleach's output is normalised and escapes `>` inside attribute values, so a regex over
# it is reliable in a way one over arbitrary HTML would not be.
_IMG_WITHOUT_SRC = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)


def memos_ng_hosts() -> frozenset[str]:
    """Every hostname this deployment is reachable by, from configuration rather than a constant.

    All three of these settings are load-bearing elsewhere (the OIDC round trip, the logout
    redirect, the iOS callback), so none of them can drift without breaking something loud — which
    is what makes them a trustworthy source for "which host is us". A literal in this module could
    go stale silently, and the test that proves the mail links nowhere here would keep passing.
    """
    sources = (
        app_settings.OIDC_REDIRECT_URI,
        app_settings.OIDC_POST_LOGOUT_REDIRECT_URI,
        app_settings.NATIVE_CALLBACK_URL,
    )
    hosts = set()
    for source in sources:
        host = urlsplit(source).hostname
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


def _points_at(value: str, hosts: frozenset[str]) -> bool:
    """Whether an href/src value resolves to one of `hosts`.

    Protocol-relative (`//host/…`) counts: a mail client resolves it against the message's own
    base, and it is a memos-ng URL by any reading. A relative path does not — it has no host and
    cannot navigate anywhere from an inbox.
    """
    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    host = urlsplit(candidate).hostname
    return host is not None and host.lower() in hosts


def sanitize_markdown(text: str, blocked_hosts: frozenset[str] = frozenset()) -> str:
    """Memo Markdown as the sanitised HTML the mail carries, with memos-ng links neutralised.

    The stripping happens at the attribute level, inside the sanitiser rather than after it: the
    allow-list is already deciding which attributes survive, and one decision point cannot
    disagree with itself. An `<a>` whose `href` was refused stays as its own text (the words are
    the memo's content and belong in the mail); an `<img>` whose `src` was refused is removed
    outright, because an image element with no source is a broken icon in every mail client.
    """

    def allowed(tag: str, name: str, value: str) -> bool:
        if name not in ALLOWED_ATTRS.get(tag, ()):
            return False
        if name in ("href", "src") and _points_at(value, blocked_hosts):
            return False
        return True

    html = markdown.markdown(text, extensions=["tables", "fenced_code"])
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=allowed)
    return _IMG_WITHOUT_SRC.sub("", cleaned)


@dataclass
class InlineImage:
    """One `cid:` part of the message: the bytes and what refers to them."""

    cid: str
    data: bytes
    content_type: str = "image/jpeg"


@dataclass
class CardImage:
    cid: str
    alt: str
    # Set only when `link_images_full` is on AND the full-size copy is hosted: the inline image
    # becomes a link to it.
    link_url: str | None = None
    # The size the encoded bytes actually decode to — NOT the size it is displayed at. The
    # template derives the display height from these and its own layout width, which is why they
    # survive the splitter's shedding: shedding a photo can turn a grid card back into a
    # single-photo card, and a height baked in at build time would then be the wrong one.
    pixel_width: int | None = None
    pixel_height: int | None = None


@dataclass
class CardVideo:
    filename: str
    status: str
    url: str | None = None
    poster_cid: str | None = None
    poster_width: int | None = None
    poster_height: int | None = None


def display_height(
    pixel_width: int | None, pixel_height: int | None, display_width: int
) -> int | None:
    """The `height` attribute that keeps an image's aspect ratio at `display_width`, or None.

    Stated explicitly because a mail client that has not loaded the images yet has no other way to
    know how much room to leave: without it the card reflows the moment the pictures arrive, and
    in the clients that block remote content by default it never stops being a 1 px sliver. The
    style keeps `height:auto` alongside it, so a client that DOES honour CSS scales the image
    correctly when the 600 px container narrows on a phone — the attribute is the fallback, not
    the instruction.

    None when the size is not known (a card built before the encoder ran, or a degenerate image):
    the template then omits the attribute rather than asserting a height that is a guess.
    """
    if not pixel_width or not pixel_height or pixel_width <= 0 or pixel_height <= 0:
        return None
    # Half-UP, not `round`: `round` is banker's rounding, so a ratio landing exactly on .5 would
    # go down for one image and up for the next. A height attribute is read by a person comparing
    # it against a ratio, and "sometimes down" is the kind of surprise that reads as a bug.
    return max(1, int(display_width * pixel_height / pixel_width + 0.5))


@dataclass
class ShedImage:
    """A photo the splitter had to drop from a too-large card, kept as a link when one exists."""

    alt: str
    url: str


@dataclass
class Card:
    uid: str
    memo_id: int
    content_html: str
    created_at: datetime
    # The memo's tags, shown as chips below the body — and, because the body no longer carries a
    # tag-only line, the ONLY place the mail states them. Both alternatives render them.
    tags: tuple[str, ...] = ()
    images: list[CardImage] = field(default_factory=list)
    videos: list[CardVideo] = field(default_factory=list)
    shed_links: list[ShedImage] = field(default_factory=list)
    overflow_count: int = 0
    # Media that never became ready before the grace ran out (selection's flag).
    is_incomplete: bool = False
    # Media that WAS ready but was shed to bring an oversized card under `max_email_bytes`, with
    # no hosted link to offer instead. A different fact from `is_incomplete` and it reads
    # differently to the household, so it is a different note.
    media_incomplete: bool = False

    def photo_cids(self) -> list[str]:
        return [image.cid for image in self.images]

    def poster_cids(self) -> list[str]:
        return [video.poster_cid for video in self.videos if video.poster_cid]


@dataclass
class DayGroup:
    date_label: str
    iso: str
    cards: list[Card] = field(default_factory=list)


def group_by_local_day(cards: Iterable[Card], timezone: str) -> list[DayGroup]:
    """Cards grouped by their memo's LOCAL calendar date, newest day first (satellite parity)."""
    zone = resolve_zone(timezone) or ZoneInfo(FALLBACK_TIMEZONE)
    buckets: dict[str, list[Card]] = {}
    labels: dict[str, str] = {}
    for card in cards:
        local = card.created_at.astimezone(zone)
        key = local.date().isoformat()
        buckets.setdefault(key, []).append(card)
        labels.setdefault(key, local.strftime("%A, %B %-d, %Y"))
    return [
        DayGroup(date_label=labels[key], iso=key, cards=buckets[key])
        for key in sorted(buckets, reverse=True)
    ]


def subject_date_range(day_groups: list[DayGroup]) -> str:
    """The satellite's date range, exactly (`app/render.py:63-72`) — it is part of the subject
    line the household recognises."""
    if not day_groups:
        return ""
    dates = sorted(date.fromisoformat(group.iso) for group in day_groups)
    lo, hi = dates[0], dates[-1]
    if lo == hi:
        return lo.strftime("%B %-d, %Y")
    if lo.year == hi.year and lo.month == hi.month:
        return f"{lo.strftime('%B %-d')} – {hi.strftime('%-d, %Y')}"
    return f"{lo.strftime('%B %-d')} – {hi.strftime('%B %-d, %Y')}"


def subject_for(mail_title: str, date_range: str, part_index: int, part_total: int) -> str:
    """`"<mail_title> — <date range>"`, with `" (Part X of Y)"` appended only when there is more
    than one part (`app/__main__.py:252`). The single-part subject must stay byte-identical to
    what the household has been receiving."""
    base = f"{mail_title} — {date_range}" if date_range else mail_title
    return f"{base} (Part {part_index} of {part_total})" if part_total > 1 else base


_VIDEO_REF = "!\\[[^\\]]*\\]\\([^)]*{uid}[^)]*\\)\\s*"


def strip_video_refs(content: str, video_uids: Iterable[str]) -> str:
    """Remove the Markdown image refs that point at a video attachment.

    The SPA renders those as players; a mail cannot, and leaving them in produces a broken image
    where the poster + play badge is about to appear anyway (satellite `app/render.py:136-140`).
    """
    for uid in video_uids:
        content = re.sub(_VIDEO_REF.format(uid=re.escape(uid)), "", content)
    return content


def card_source_paths(
    candidates: list[Candidate], links: MediaLinks, max_videos_per_memo: int
) -> list[Path]:
    """Every local file the cards will inline FROM: a mobile tier per photo, a poster per HOSTED
    video.

    A poster is read only for a video that has a link, because that is the only branch the
    template renders one in — an unhosted video is a chip, not a picture.
    """
    paths: list[Path] = []
    for candidate in candidates:
        for image in candidate.images:
            resolved = derived_tier_path(image.uid, MAIL_IMAGE_TIER)
            if resolved is not None:
                paths.append(resolved[0])
        for video in candidate.videos[:max_videos_per_memo]:
            if links.by_attachment.get(video.attachment_id) is None:
                continue
            resolved = derived_tier_path(video.uid, "poster")
            if resolved is not None:
                paths.append(resolved[0])
    return paths


def _encode_files_sync(paths: list[Path], profile: MailProfile) -> dict[Path, media.MailImage]:
    """Read every inline source and re-encode it for the mail, in ONE offloaded hop.

    Both halves belong in the same hop and neither belongs on the loop: on the deployed bind mount
    each read is a network round trip, and each encode is a decode + LANCZOS reduction + JPEG
    write that occupies a core for as long as it takes. A run does dozens of both.

    A source that will not read or will not decode is simply absent from the result, which is the
    degradation `build_cards` already handles — one photo leaves its card, the mail goes out.
    """
    encoded: dict[Path, media.MailImage] = {}
    for path in paths:
        try:
            raw = path.read_bytes()
        except OSError:
            continue  # a tier that vanished between the readiness scan and now: card degrades
        image = media.encode_for_mail(raw, profile)
        if image is not None:
            encoded[path] = image
    return encoded


async def read_card_images(
    paths: list[Path], profile: MailProfile
) -> dict[Path, media.MailImage]:
    """Every inline image of the run, encoded to the MAIL profile it was handed.

    The profile is a parameter and never a lookup: it is the run's snapshot, read once before any
    of this, so a settings change between the first photo and the last cannot put two encodings in
    one message.
    """
    return await to_thread.run_sync(_encode_files_sync, paths, profile)


def build_cards(
    candidates: list[Candidate],
    files: dict[Path, media.MailImage],
    links: MediaLinks,
    *,
    max_videos_per_memo: int,
    link_images_full: bool,
    blocked_hosts: frozenset[str],
) -> tuple[list[Card], list[InlineImage]]:
    """The satellite's `prepare_memo_cards`, as a pure function over already-read bytes.

    Purity is what makes the oversized-card, shedding and CID-accounting tests possible without a
    filesystem — and the CID accounting is what a real failure bought: a poster CID left out of a
    part's attachment set renders as a broken thumbnail in the mail.
    """
    cards: list[Card] = []
    inline: list[InlineImage] = []
    for candidate in candidates:
        content = strip_video_refs(candidate.content, (v.uid for v in candidate.videos))
        # The SPA's rule, applied to the SAME text python-markdown is about to parse: a line that
        # is nothing but hashtags is a heading to every Markdown parser and has never been one in
        # any other client (MEMNG-275). The tags reach the reader as chips instead, below.
        content = strip_tag_only_lines(content)
        card = Card(
            uid=candidate.uid,
            memo_id=candidate.memo_id,
            content_html=sanitize_markdown(content, blocked_hosts),
            created_at=candidate.created_at,
            tags=candidate.tags,
            is_incomplete=candidate.incomplete,
        )
        for image in candidate.images:
            resolved = derived_tier_path(image.uid, MAIL_IMAGE_TIER)
            encoded = None if resolved is None else files.get(resolved[0])
            if encoded is None:
                continue
            cid = f"img-{image.uid}-{uuid.uuid4().hex[:8]}"
            inline.append(InlineImage(cid=cid, data=encoded.data))
            link = links.by_attachment.get(image.attachment_id) if link_images_full else None
            card.images.append(
                CardImage(
                    cid=cid,
                    alt=image.filename or "photo",
                    link_url=link,
                    pixel_width=encoded.width,
                    pixel_height=encoded.height,
                )
            )
        for video in candidate.videos[:max_videos_per_memo]:
            url = links.by_attachment.get(video.attachment_id)
            if url is None:
                status = (
                    PROCESSING if video.attachment_id in links.processing else NOT_HOSTED
                )
                card.videos.append(CardVideo(filename=video.filename, status=status))
                continue
            poster_cid = None
            poster_size: tuple[int | None, int | None] = (None, None)
            resolved = derived_tier_path(video.uid, "poster")
            encoded = None if resolved is None else files.get(resolved[0])
            if encoded is not None:
                poster_cid = f"poster-{video.uid}-{uuid.uuid4().hex[:8]}"
                inline.append(InlineImage(cid=poster_cid, data=encoded.data))
                poster_size = (encoded.width, encoded.height)
            card.videos.append(
                CardVideo(
                    filename=video.filename,
                    status=HOSTED,
                    url=url,
                    poster_cid=poster_cid,
                    poster_width=poster_size[0],
                    poster_height=poster_size[1],
                )
            )
        cards.append(card)
    return cards, inline


@lru_cache(maxsize=1)
def _template() -> Template:
    """The compiled mail template, built and loaded ONCE per process.

    Both halves matter and both used to happen on every single render: constructing an
    `Environment` compiles nothing but does set up a fresh loader and an empty template cache, and
    `get_template` then reads and compiles the template off the filesystem — on the deployed
    host, off a network bind mount, inside the event loop, once per PART of every mail. Caching
    the compiled template makes every render after the first one pure CPU over an in-memory
    object, which is why this stays synchronous rather than being offloaded: there is no
    filesystem call left in it to offload.

    `auto_reload=False` is what keeps that true — Jinja otherwise stats the source file on every
    lookup to decide whether to recompile. The template is shipped inside the image and cannot
    change under a running process.
    """
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        auto_reload=False,
    )
    # The layout decides the display width, so the layout computes the height — a Python helper
    # rather than arithmetic in the markup, so it can be tested directly and so the two widths the
    # template lays out at are the only numbers the template itself has to know.
    environment.globals["display_height"] = display_height
    environment.globals["single_image_width"] = SINGLE_IMAGE_WIDTH
    environment.globals["grid_image_width"] = GRID_IMAGE_WIDTH
    return environment.get_template(TEMPLATE_NAME)


def render_html(
    day_groups: list[DayGroup],
    *,
    subject: str,
    mail_title: str,
    date_range: str,
    memo_count: int,
    part_index: int = 1,
    part_total: int = 1,
) -> str:
    return _template().render(
        day_groups=day_groups,
        subject=subject,
        mail_title=mail_title,
        date_range=date_range,
        memo_count=memo_count,
        part_index=part_index,
        part_total=part_total,
    )


def render_plain(
    day_groups: list[DayGroup],
    *,
    subject: str,
    memo_count: int,
    part_index: int = 1,
    part_total: int = 1,
) -> str:
    """The `text/plain` alternative (satellite `app/render.py:212-239`).

    Not decoration: a message that offers only `text/html` reads as spam-shaped to several
    filters, and the household's own archive search runs over the text part.
    """
    lines = [subject, "=" * len(subject), ""]
    if part_total > 1:
        lines += [f"Part {part_index} of {part_total}", ""]
    for day in day_groups:
        lines += [day.date_label, "-" * len(day.date_label)]
        for card in day.cards:
            lines.append("")
            text = card.content_html.replace("<br>", "\n").replace("</p>", "\n")
            text = bleach.clean(text, tags=[], strip=True).strip()
            if text:
                lines.append(text)
            if card.tags:
                # The tags are not in `content_html` any more (the tag-only line was stripped), and
                # this alternative is what the household's archive search reads — so they are
                # stated here, below the text, or the text part would silently lose them.
                lines.append(" ".join(f"#{tag}" for tag in card.tags))
            if card.images:
                lines.append(f"  [{len(card.images)} image(s)]")
            for video in card.videos:
                if video.status == HOSTED:
                    lines.append(f"  [video: {video.filename} — {video.url or ''}]")
                elif video.status == PROCESSING:
                    lines.append(f"  [video processing: {video.filename}]")
                else:
                    lines.append(f"  [video: {video.filename} — not hosted]")
            for shed in card.shed_links:
                lines.append(f"  [full size: {shed.url}]")
            if card.overflow_count:
                lines.append(f"  +{card.overflow_count} more (not shown)")
        lines.append("")
    lines.append(f"{memo_count} new memo(s) across {len(day_groups)} day(s)")
    return "\n".join(lines)


EMPTY_BODY_NOTE = "No memos selected."


def render_empty(mail_title: str) -> tuple[str, str]:
    """The "no memos selected" body a test-send falls back to, so it is always a usable SMTP probe
    (and so the probe needs no settings write anywhere in it)."""
    html = render_html(
        [],
        subject=mail_title,
        mail_title=mail_title,
        date_range="",
        memo_count=0,
    )
    plain = "\n".join([mail_title, "=" * len(mail_title), "", EMPTY_BODY_NOTE])
    return html, plain
