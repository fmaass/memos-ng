"""The small link-mode mail (MEMNG-1246, D5): one preview, the newest day, one link to its page.

What it carries, per language group: one inline `cid:` preview under `PREVIEW_MAX_BYTES`, the
target day's first memo text, that day's counts, an Open button, the bare URL, an "online until"
line and — when the run spans more days — one line "and N older days". The target day is the
NEWEST local day of the run; every count, the preview and the text come from that day alone, and
the older days are walked from the page's own previous link.

It never goes through the splitter: there is exactly one small image, so there is nothing to
split. Memo content still goes through the same sanitiser and the same memos-ng host strip as the
attachments mail (`render.build_cards`), so a memo's link back to memos-ng is stripped here too,
while a link to the door's host survives because it is not a memos-ng host (and the door host is
refused outright if it ever is one, `publish.page_host_problem`).
"""

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import bleach
from anyio import to_thread
from jinja2 import Environment, FileSystemLoader, Template, select_autoescape

from app.services import media
from app.services.asset_paths import derived_tier_path
from app.services.digest import i18n
from app.services.digest.gate import FALLBACK_TIMEZONE, resolve_zone
from app.services.digest.publish import DayPublication
from app.services.digest.render import TEMPLATE_DIR, Card, DayGroup, InlineImage
from app.services.digest.select import MAIL_IMAGE_TIER, Candidate
from app.services.export_settings import MailProfile

LINK_TEMPLATE_NAME = "link.html.j2"
PREVIEW_CID = "preview"
# D5: the one inline picture stays under 100 KB, whatever the source. The encoder walks down this
# ladder — long edge first held, quality lowered — until the bytes fit.
PREVIEW_MAX_BYTES = 100_000
PREVIEW_LONG_EDGES = (600, 480, 360, 240)
PREVIEW_QUALITIES = (80, 70, 60, 50)
PREVIEW_DISPLAY_WIDTH = 540


@dataclass
class DayCounts:
    memos: int
    photos: int
    videos: int


@dataclass
class LinkMail:
    """Everything the link template needs, decided once per group."""

    day: DayGroup
    publication: DayPublication
    counts: DayCounts
    older_days: int
    first_card: Card
    preview: InlineImage | None = None
    preview_width: int | None = None
    preview_height: int | None = None


def target_day(day_groups: list[DayGroup]) -> tuple[DayGroup, int]:
    """The newest local day and how many older days the run also touched.

    `day_groups` comes from `render.group_by_local_day`, newest first; the run is never empty here
    (an empty selection has no mail)."""
    if not day_groups:
        raise ValueError("a link mail needs at least one day")
    return day_groups[0], len(day_groups) - 1


def day_candidates(candidates: list[Candidate], day: DayGroup) -> list[Candidate]:
    """The candidates whose memo is on `day`, in the day's own card order."""
    memo_ids = [card.memo_id for card in day.cards]
    by_id = {candidate.memo_id: candidate for candidate in candidates}
    return [by_id[memo_id] for memo_id in memo_ids if memo_id in by_id]


def day_counts(candidates: list[Candidate], max_videos_per_memo: int) -> DayCounts:
    """What the target day holds — the same `max_videos_per_memo` bound the page shows."""
    return DayCounts(
        memos=len(candidates),
        photos=sum(len(candidate.images) for candidate in candidates),
        videos=sum(len(candidate.videos[:max_videos_per_memo]) for candidate in candidates),
    )


def preview_source(candidates: list[Candidate]) -> Path | None:
    """The file the preview is made from: the day's first photo, else its first video poster."""
    for candidate in candidates:
        for image in candidate.images:
            resolved = derived_tier_path(image.uid, MAIL_IMAGE_TIER)
            if resolved is not None and resolved[0].is_file():
                return resolved[0]
    for candidate in candidates:
        for video in candidate.videos:
            resolved = derived_tier_path(video.uid, "poster")
            if resolved is not None and resolved[0].is_file():
                return resolved[0]
    return None


def encode_preview(source: bytes, *, subsampling: str) -> media.MailImage | None:
    """The source re-encoded under `PREVIEW_MAX_BYTES`, or None when it will not decode or no rung
    of the ladder fits. None costs the mail its picture, never the mail itself."""
    for edge in PREVIEW_LONG_EDGES:
        for quality in PREVIEW_QUALITIES:
            profile = MailProfile(
                long_edge=edge, jpeg_quality=quality, subsampling=subsampling, progressive=True
            )
            image = media.encode_for_mail(source, profile)
            if image is None:
                return None
            if len(image.data) < PREVIEW_MAX_BYTES:
                return image
    return None


def _read_preview_sync(path: Path, subsampling: str) -> media.MailImage | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return encode_preview(raw, subsampling=subsampling)


async def read_preview(path: Path | None, subsampling: str) -> media.MailImage | None:
    """Read and encode the preview off the loop: a network read and a decode + reduce + encode."""
    if path is None:
        return None
    return await to_thread.run_sync(_read_preview_sync, path, subsampling)


def counts_line(counts: DayCounts, language: str) -> str:
    """`1 memo · 3 photos · 1 video`; a kind the day does not have is left out."""

    def plural(kind: str, count: int) -> str:
        suffix = "one" if count == 1 else "other"
        return i18n.text(language, f"link.{kind}_{suffix}", count=count)

    pieces = [plural("memos", counts.memos)]
    if counts.photos:
        pieces.append(plural("photos", counts.photos))
    if counts.videos:
        pieces.append(plural("videos", counts.videos))
    return " · ".join(pieces)


def older_days_line(older_days: int, language: str) -> str:
    if older_days <= 0:
        return ""
    key = "link.older_days_one" if older_days == 1 else "link.older_days_other"
    return i18n.text(language, key, count=older_days)


def online_until_line(link: LinkMail, timezone: str, language: str) -> str:
    """The day the page goes away, in the digest timezone — the household's own calendar."""
    zone = resolve_zone(timezone) or ZoneInfo(FALLBACK_TIMEZONE)
    local: date = link.publication.online_until.astimezone(zone).date()
    return i18n.text(language, "link.online_until", date=i18n.long_date(local, language))


def plain_text(content_html: str) -> str:
    """The memo body as the text alternative carries it — the attachments mail's own reduction."""
    text = content_html.replace("<br>", "\n").replace("</p>", "\n")
    return bleach.clean(text, tags=[], strip=True).strip()


@lru_cache(maxsize=1)
def _template() -> Template:
    """Compiled once per process, for the same reasons as `render._template`."""
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        auto_reload=False,
    )
    return environment.get_template(LINK_TEMPLATE_NAME)


def _display_height(link: LinkMail) -> int | None:
    width, height = link.preview_width, link.preview_height
    if not width or not height:
        return None
    return max(1, int(PREVIEW_DISPLAY_WIDTH * height / width + 0.5))


def render_html(
    link: LinkMail, *, subject: str, mail_title: str, date_range: str, timezone: str, language: str
) -> str:
    def t(key: str, **values: object) -> str:
        return i18n.text(language, key, **values)

    return _template().render(
        t=t,
        language=language,
        subject=subject,
        mail_title=mail_title,
        date_range=date_range,
        day_label=link.day.date_label,
        first_html=link.first_card.content_html,
        counts=counts_line(link.counts, language),
        older=older_days_line(link.older_days, language),
        url=link.publication.url,
        online_until=online_until_line(link, timezone, language),
        preview_cid=link.preview.cid if link.preview else None,
        preview_width=PREVIEW_DISPLAY_WIDTH,
        preview_height=_display_height(link),
    )


def render_plain(link: LinkMail, *, subject: str, timezone: str, language: str) -> str:
    """The text alternative: the same words and the same one URL, no markup."""
    lines = [subject, "=" * len(subject), "", link.day.date_label, "-" * len(link.day.date_label)]
    text = plain_text(link.first_card.content_html)
    if text:
        lines += ["", text]
    lines += ["", counts_line(link.counts, language)]
    older = older_days_line(link.older_days, language)
    if older:
        lines.append(older)
    lines += [
        "",
        f"{i18n.text(language, 'link.open')}: {link.publication.url}",
        online_until_line(link, timezone, language),
    ]
    return "\n".join(lines)
