"""The admin-only API over the export image profiles (MEMNG-238).

One resource, two verbs, behind `require_admin` — which is built on `require_session_user`, so an
access token never reaches here (the bearer is refused before the cookie is read) and a non-admin
session is answered 403. The Export tab hides itself for a non-admin as a courtesy; the refusal
happens here, at the sink.

There is no create path, deliberately: `CHECK (id = 1)` on the table plus a router that only ever
UPDATEs is what makes a second settings row impossible. Either alone would be a convention.

Both profiles live on one row because they are one decision — how this deployment encodes an image
for somewhere that is not a screen — and the row is what a PRINT rendition's cache key is derived
from, so the response says which key the current values resolve to rather than leaving every
consumer to re-derive it.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import ExportSettings, MailSubsampling, PrintMetadataPolicy, User
from app.services import export_settings as export_settings_svc

router = APIRouter(prefix="/api/admin/export", tags=["admin-export"])


# --- schemas ----------------------------------------------------------------------------------


class ExportSettingsOut(BaseModel):
    print_long_edge: int
    print_jpeg_quality: int
    print_metadata_policy: PrintMetadataPolicy
    print_grayscale_single_channel: bool
    # DERIVED, read-only: the cache key the four print fields above resolve to. It is published
    # because it is the only thing that says whether a change will re-derive anything — the
    # default values resolve to `print-4096` and serve the tier that is already on disk, and every
    # other combination names a directory of its own.
    print_profile_key: str
    mail_long_edge: int
    mail_jpeg_quality: int
    mail_subsampling: MailSubsampling
    mail_progressive: bool
    updated_at: Rfc3339Datetime
    updated_by: str | None


class ExportSettingsPut(BaseModel):
    """The WHOLE settings row, not a patch — the panel saves both profiles behind one Save bar, so
    the request IS the new state and an omitted field is a client bug (pydantic answers 422).

    Every band here is the vendor-documented envelope, not a plausibility limit, and each is the
    exact set the Export panel offers: print quality below 90 shows on gloss paper and above 95
    costs bytes for nothing; the print long edge is the measured set of useful rungs (4096 covers a
    1.5-page spread of the 21x21 layflat at 300 dpi, 5000 is Blurb's published full-bleed rule,
    0 keeps the source's own size); the mail long edge is 2x a 600-800 px rendered width; mail
    quality 77-83 is the band mail-client practice puts an inline photo in. The API and the panel
    admit exactly the same values, so a request the panel could not have produced is a 422 rather
    than a state no UI can show.
    """

    # Literals rather than ranges: these are not points on a continuum, they are the answers the
    # design settled on, and for the print long edge every other value would silently create a
    # cache directory nobody asked for.
    print_long_edge: Literal[0, 4096, 5000]
    print_jpeg_quality: int = Field(ge=90, le=95)
    print_metadata_policy: PrintMetadataPolicy
    print_grayscale_single_channel: bool
    mail_long_edge: Literal[1200, 1600, 2048]
    mail_jpeg_quality: int = Field(ge=77, le=83)
    mail_subsampling: MailSubsampling
    mail_progressive: bool


# --- helpers ----------------------------------------------------------------------------------


async def _settings_row(db: AsyncSession, *, for_update: bool = False) -> ExportSettings:
    """`export_settings.settings_row`, with its one domain failure translated into a status code.

    The translation is the ONLY thing this adds, which is why the lookup itself lives in the
    service: the export route resolves the same row from a path that must not raise HTTP. A
    missing singleton is a broken install rather than a caller error — the migration seeds it, and
    creating one here would be the INSERT path that makes a second row possible.
    """
    try:
        return await export_settings_svc.settings_row(db, for_update=for_update)
    except export_settings_svc.SettingsMissing as exc:
        raise HTTPException(status_code=500, detail="export settings row is missing") from exc


def _settings_out(row: ExportSettings) -> ExportSettingsOut:
    return ExportSettingsOut(
        print_long_edge=row.print_long_edge,
        print_jpeg_quality=row.print_jpeg_quality,
        print_metadata_policy=PrintMetadataPolicy(row.print_metadata_policy),
        print_grayscale_single_channel=row.print_grayscale_single_channel,
        print_profile_key=export_settings_svc.ExportConfig.of(row).print_profile.key,
        mail_long_edge=row.mail_long_edge,
        mail_jpeg_quality=row.mail_jpeg_quality,
        mail_subsampling=MailSubsampling(row.mail_subsampling),
        mail_progressive=row.mail_progressive,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


# --- settings ---------------------------------------------------------------------------------


@router.get("/settings", response_model=ExportSettingsOut)
async def get_export_settings(
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ExportSettingsOut:
    return _settings_out(await _settings_row(db))


@router.put("/settings", response_model=ExportSettingsOut)
async def put_export_settings(
    body: ExportSettingsPut,
    admin: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ExportSettingsOut:
    """Update the one settings row.

    The row is taken FOR UPDATE for the whole request. Not for a fresh read: two overlapping saves
    would otherwise interleave field by field and commit a profile that is neither of the two the
    admins pressed Save on — a mixture of print quality from one and grayscale from the other,
    which is a cache key nobody chose. One row, taken in one order by the one writer, so the
    second request waits and overwrites a complete state rather than half of one.
    """
    row = await _settings_row(db, for_update=True)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    row.updated_by = admin.subject
    await db.commit()
    # `updated_at` is stamped by the database (`onupdate=func.now()`), so the flush leaves that one
    # attribute expired — and reading an expired attribute on an async session outside a greenlet
    # raises rather than lazily loading. Refetch the row explicitly.
    await db.refresh(row)
    return _settings_out(row)
