"""The two named image profiles this deployment exports with, and how a request resolves one.

One row (`export_settings`, id=1) holds both: the PRINT profile the photo book derives at, and the
MAIL profile the digest's inline images will be encoded with. They live together because they are
the same decision seen twice — how an image is prepared for somewhere that is not a screen — and
apart from the digest's operational settings, which are about when and to whom, not about bytes.

This module is the domain half, mirroring `app/services/digest/domain.py`: the row lookup, its
one failure, a frozen snapshot, and the resolution rule the export route needs. The router that
edits it (app/routers/admin_export.py) adds only status codes. A missing row is a broken install
and raises — it is never created here, because an INSERT path is exactly what would make a second
row possible (`CHECK (id = 1)` alone is a constraint; a router that only UPDATEs is the other half).

**Nothing here reads settings on behalf of a rendition that already exists.** A profile is resolved
ONCE per request and then carried as a value: into the cache path, into the job row, into the
manifest. The moment a second lookup decided any of those, a settings change between the request
and the encode would silently pick the bytes.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExportSettings
from app.services import printready

SETTINGS_ID = 1


class SettingsMissing(RuntimeError):
    """The `export_settings` singleton is not there. A broken install, not a caller error."""


class UnknownProfile(ValueError):
    """A requested profile key names no profile this deployment can serve."""


async def settings_row(db: AsyncSession, *, for_update: bool = False) -> ExportSettings:
    """The singleton, optionally LOCKED for the rest of this transaction.

    `for_update=True` is a mutex over the one row every settings mutation is interested in, not a
    way of reading a fresher value. Raises `SettingsMissing`.
    """
    statement = select(ExportSettings).where(ExportSettings.id == SETTINGS_ID)
    if for_update:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise SettingsMissing(f"export_settings row {SETTINGS_ID} is missing")
    return row


@dataclass(frozen=True, slots=True)
class MailProfile:
    """How the digest will encode an inline image. STORED and exposed by this story; the renderer
    that consumes it is a separate one, so nothing reads these values yet."""

    long_edge: int
    jpeg_quality: int
    subsampling: str
    progressive: bool


@dataclass(frozen=True, slots=True)
class ExportConfig:
    """The settings row as a plain value, read once and carried across every boundary."""

    print_profile: printready.PrintProfile
    mail_profile: MailProfile

    @classmethod
    def of(cls, row: ExportSettings) -> "ExportConfig":
        return cls(
            print_profile=printready.PrintProfile(
                long_edge=row.print_long_edge,
                jpeg_quality=row.print_jpeg_quality,
                metadata_policy=row.print_metadata_policy,
                grayscale_single_channel=row.print_grayscale_single_channel,
            ),
            mail_profile=MailProfile(
                long_edge=row.mail_long_edge,
                jpeg_quality=row.mail_jpeg_quality,
                subsampling=row.mail_subsampling,
                progressive=row.mail_progressive,
            ),
        )


async def load_config(db: AsyncSession) -> ExportConfig:
    """The settings singleton as a snapshot. Raises `SettingsMissing`."""
    return ExportConfig.of(await settings_row(db))


async def print_profile(db: AsyncSession) -> printready.PrintProfile:
    """The print profile settings currently name. Raises `SettingsMissing`."""
    return (await load_config(db)).print_profile


async def resolve_print_profile(db: AsyncSession, requested: str | None) -> printready.PrintProfile:
    """The profile a photo-book request is served at: what it asked for, or what settings say.

    `requested is None` means the parameter was OMITTED, which is not the same as asking for
    `print-4096` by name — that distinction is the entire reason the route's default is None. An
    omission follows settings, so changing the print profile changes what every export produces
    without any client being told; an explicit key pins the request to that profile whatever
    settings say, which is what makes an already-downloaded book reproducible.

    Exactly two keys resolve: the DEFAULT profile (permanently addressable — its cache is the
    shipped tier and never goes stale) and whatever settings name right now. A key that named a
    profile the admin has since moved away from is refused rather than silently re-derived: those
    files still exist, but nothing here is a general-purpose "encode it however I ask" endpoint,
    and answering an arbitrary key would let any caller queue arbitrary 60 MP re-encodes on a
    thermally bound host. Raises `UnknownProfile`; `SettingsMissing` propagates.
    """
    configured = await print_profile(db)
    if requested is None:
        return configured
    for candidate in (printready.DEFAULT_PROFILE, configured):
        if candidate.key == requested:
            return candidate
    raise UnknownProfile(requested)
