"""The seam between a link-mode run and the door that hosts the day pages (MEMNG-1246).

A link-mode mail points at ONE day's page. Making that page exist — pushing the day's manifest to
the door, which mints the page's unlisted id and its `published_at` — is MEMNG-1247. This module
is the narrow contract the run needs from it, and nothing more: hand over the local days the run
touches, get back one `DayPublication` per day.

The only production implementation today is `NotConfiguredPublisher`. A link-mode run that meets
it is DEFERRED — no mail, no delivery rows — and is never quietly sent in attachments mode
instead: the admin chose the small mail, and a mail of a different kind arriving unannounced is
the surprise this switch exists to avoid.

Whatever a publisher answers is checked against the configured page host before any mail is
built: a link-mode mail may point at the door and at nothing else of the household's.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from app.config import settings as app_settings
from app.services.digest.render import memos_ng_hosts

ERROR_PUBLISHER_UNCONFIGURED = "link mode: the day-page publisher is not configured"
ERROR_PAGE_HOST_UNCONFIGURED = "link mode: DAILY_PAGE_HOST is not configured"
ERROR_PAGE_HOST_IS_MEMOS_NG = "link mode: DAILY_PAGE_HOST is a memos-ng host"


class PublisherUnavailable(RuntimeError):
    """The day pages cannot be published, so a link-mode run has nothing to link to."""


@dataclass(frozen=True)
class DayPublication:
    """One local day's page on the door: what the mail links to and until when it answers.

    `published_at` is the door's clock, minted on the day's FIRST publication and kept on every
    later one (MEMNG-1247 mints it). Home stores it on `daily_pushes` and stamps every hosted file
    of the day `published_at + retention_days` from it (MEMNG-1244), so page and files share one
    window."""

    day: date
    url: str
    online_until: datetime
    published_at: datetime


class DayPublisher(Protocol):
    """Publish the pages of `days` (the run's local days, oldest first) and answer one
    `DayPublication` per day. Idempotent per day: a day published before keeps its url."""

    async def publish(self, days: Sequence[date]) -> Sequence[DayPublication]: ...


class NotConfiguredPublisher:
    """The production publisher until MEMNG-1247 lands: there is no door to publish to."""

    async def publish(self, days: Sequence[date]) -> Sequence[DayPublication]:
        raise PublisherUnavailable(ERROR_PUBLISHER_UNCONFIGURED)


def default_publisher() -> DayPublisher:
    return NotConfiguredPublisher()


def page_host() -> str:
    """The configured door hostname, normalised the way `memos_ng_hosts` normalises its own."""
    return app_settings.DAILY_PAGE_HOST.strip().lower()


def page_host_problem() -> str | None:
    """Why link mode cannot run with this configuration, or None when it can.

    A page host that is one of memos-ng's own would turn the household's one public link into a
    link back here — exactly what the host strip exists to prevent — so it is refused outright
    rather than allowed through the strip."""
    host = page_host()
    if not host:
        return ERROR_PAGE_HOST_UNCONFIGURED
    if host in memos_ng_hosts():
        return ERROR_PAGE_HOST_IS_MEMOS_NG
    return None


def checked_publications(
    publications: Sequence[DayPublication], days: Sequence[date]
) -> dict[date, DayPublication]:
    """The publisher's answer by day, refusing anything the mail must not carry: a day it did not
    answer, or a url that is not `https://<page host>/…`."""
    host = page_host()
    by_day = {publication.day: publication for publication in publications}
    for day in days:
        publication = by_day.get(day)
        if publication is None:
            raise PublisherUnavailable(f"link mode: no page was published for {day.isoformat()}")
        parts = urlsplit(publication.url)
        if parts.scheme != "https" or (parts.hostname or "").lower() != host:
            raise PublisherUnavailable(
                f"link mode: the page for {day.isoformat()} is not on the page host"
            )
    return by_day


def probe_publications(
    days: Sequence[date], *, now: datetime, retention_days: int
) -> Mapping[date, DayPublication]:
    """What a preview or a test-send links to: the door's front page, published nowhere.

    A probe persists nothing — no run row, no upload, and so no page either. The door's root is a
    real page (its sign-in), which keeps the probe's one link working rather than a made-up id."""
    url = f"https://{page_host()}/"
    until = now + timedelta(days=retention_days)
    return {
        day: DayPublication(day=day, url=url, online_until=until, published_at=now) for day in days
    }
