"""The digest's domain facts: the settings singleton, address normalisation, capability checks.

Three consumers need the same three answers — the admin router, the run pipeline and the one-shot
import script — and before this module existed the script reached into the router for them
(`from app.routers.admin_digest import SETTINGS_ID, _settings_row, normalise_email`) and caught
`HTTPException` from a process that serves no HTTP. That is the layering this module ends: nothing
here imports FastAPI, so a missing singleton is a domain error (`SettingsMissing`) that the router
translates into a status code and the script reports as a skipped section.

`DigestConfig` is the other half. The pipeline reads the settings ONCE, into a frozen plain value,
and then works from that value across filesystem, Dropbox and SMTP boundaries — because a mapped
`DigestSettings` instance keeps a session (and therefore a pooled connection and an open READ
COMMITTED transaction) alive for exactly as long as somebody might read an attribute off it. A
snapshot cannot: it is a dataclass, it outlives its session by construction, and holding one is
provably not holding a connection.
"""

import re
from dataclasses import dataclass
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.models import DigestDeliveryMode, DigestSettings

# The settings table holds exactly one row, and this is its id. `CHECK (id = 1)` in the migration
# makes a second row impossible at the database; every writer UPDATEs this row and none of them
# INSERTs, which is the other half of that guarantee.
SETTINGS_ID = 1

# RFC 5321's addr-spec ceiling, and the width of the column.
MAX_EMAIL_LENGTH = 320

# Deliberately NOT `pydantic.EmailStr`: that would add the `email-validator` dependency for a check
# this does not need. What is needed is the RFC 5322 addr-spec SHAPE plus one normalisation rule —
# a domain is case-insensitive and a household will type the same address two ways — so the whole
# address is stored lower-cased. That normalisation is what makes the plain UNIQUE index on
# `digest_recipients.email` a case-INSENSITIVE one: two spellings collapse to one stored string.
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]"
_EMAIL_RE = re.compile(
    rf"^{_ATEXT}+(?:\.{_ATEXT}+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


class SettingsMissing(RuntimeError):
    """The `digest_settings` singleton is absent. The migration seeds it, so this is a broken
    install rather than a caller error — and creating one here would be the INSERT path that makes
    a second row possible."""


def normalise_email(raw: str) -> str | None:
    """`raw` trimmed and lower-cased, or None when it is not an address this API will store.

    A pure function, and tested as one: it is the single point where "is this an address?" and
    "which stored string does it become?" are decided, so the dedupe rule and the rejection rule
    can never disagree about a spelling.
    """
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(candidate):
        return None
    return candidate.lower()


def smtp_configured() -> bool:
    """Whether a mail can be sent at all. Presence, never the value."""
    return bool(app_settings.SMTP_USERNAME and app_settings.SMTP_PASSWORD)


def dropbox_configured() -> bool:
    """Whether media hosting can work at all.

    All three keys are required, because two out of three cannot refresh a token, and an empty key
    is the switch that disables hosting globally. Presence, never the value: the keys are secrets
    and nothing about them is echoed anywhere.
    """
    return all(
        (
            app_settings.DROPBOX_APP_KEY,
            app_settings.DROPBOX_APP_SECRET,
            app_settings.DROPBOX_REFRESH_TOKEN,
        )
    )


async def settings_row(db: AsyncSession, *, for_update: bool = False) -> DigestSettings:
    """The singleton, optionally LOCKED for the rest of this transaction.

    `for_update=True` is not about reading a fresh value — it is a mutex over the one row every
    recipient/settings mutation is already interested in. Raises `SettingsMissing`.
    """
    statement = select(DigestSettings).where(DigestSettings.id == SETTINGS_ID)
    if for_update:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise SettingsMissing(f"digest_settings row {SETTINGS_ID} is missing")
    return row


@dataclass(frozen=True)
class DigestConfig:
    """The settings row as a plain value, read once and carried across every I/O boundary.

    Frozen because a run must decide its policy once: a caps or timezone change committed halfway
    through a send would otherwise apply to some parts and not others.
    """

    enabled: bool
    send_time: time
    timezone: str
    visibility_scope: str
    start_date: date
    grace_minutes: int
    max_email_bytes: int
    max_videos_per_memo: int
    max_memos_per_run: int
    mail_title: str
    from_name: str
    from_email: str
    hosting_enabled: bool
    dropbox_folder: str
    retention_days: int
    link_images_full: bool
    # Defaulted so a snapshot built field by field elsewhere keeps meaning today's mail.
    delivery_mode: str = DigestDeliveryMode.attachments.value

    @classmethod
    def of(cls, row: DigestSettings) -> "DigestConfig":
        return cls(
            enabled=row.enabled,
            send_time=row.send_time,
            timezone=row.timezone,
            visibility_scope=row.visibility_scope,
            start_date=row.start_date,
            grace_minutes=row.grace_minutes,
            max_email_bytes=row.max_email_bytes,
            max_videos_per_memo=row.max_videos_per_memo,
            max_memos_per_run=row.max_memos_per_run,
            mail_title=row.mail_title,
            from_name=row.from_name,
            from_email=row.from_email,
            hosting_enabled=row.hosting_enabled,
            dropbox_folder=row.dropbox_folder,
            retention_days=row.retention_days,
            link_images_full=row.link_images_full,
            delivery_mode=row.delivery_mode,
        )


async def load_config(db: AsyncSession) -> DigestConfig:
    """The settings singleton as a snapshot. Raises `SettingsMissing`."""
    return DigestConfig.of(await settings_row(db))


def sender_address(config: DigestConfig) -> str:
    """`from_email`, or the address that actually authenticates when it is empty (the seeded
    value's documented meaning)."""
    return config.from_email or app_settings.SMTP_USERNAME
