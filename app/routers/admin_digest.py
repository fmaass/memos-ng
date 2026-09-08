"""The admin-only API over the daily digest's state (MEMNG-233).

Everything here sits behind `require_admin`, which is built on `require_session_user` — so an
access token never reaches these routes (the bearer is refused before the cookie is read) and a
non-admin session is answered 403. That is the whole authorization story for the feature: the
Mail panel hides itself for a non-admin as a courtesy, but the refusal happens here, at the sink.

Three ACTION endpoints sit beside the state ones — preview, test-send and run-now — and each is
deliberately explicit about what it persists. Preview and test-send persist NOTHING: not a run
row, not a hosted-media row, not a Dropbox file. Run-now persists exactly one thing, the run row
that IS the single-flight lock, and then answers 202: the pipeline behind it takes minutes to
hours, so it is EXECUTED by the worker, through the very same `runner.py` a scheduled run uses. An
endpoint that awaited it would make a browser tab, a proxy timeout or a reload the thing that
decides whether the household gets its mail.
"""

from datetime import UTC, date, datetime, time
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.config import settings as app_settings
from app.database import async_session, get_session
from app.models import (
    DigestRecipient,
    DigestRun,
    DigestSettings,
    DigestTrigger,
    DigestVisibilityScope,
    User,
)
from app.services.cursors import InvalidCursor, decode_cursor_payload, encode_cursor_payload
from app.services.digest import domain as digest_domain
from app.services.digest import hosting as digest_hosting
from app.services.digest import metrics as digest_metrics
from app.services.digest import runner as digest_runner
from app.services.digest.domain import MAX_EMAIL_LENGTH, normalise_email
from app.services.digest.gate import local_date_of

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin/digest", tags=["admin-digest"])

# The singleton id, the address ceiling, the address rule and the capability checks all live in
# `app.services.digest.domain`: the run pipeline and the one-shot import script need the same
# answers, and a router is the wrong place for three consumers to reach into.


# --- schemas ----------------------------------------------------------------------------------


class DigestSettingsOut(BaseModel):
    enabled: bool
    send_time: time
    timezone: str
    visibility_scope: DigestVisibilityScope
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
    updated_at: Rfc3339Datetime
    updated_by: str | None


# One Save bar for the whole panel is the digest design's shape (3285 §6).
class DigestSettingsPut(BaseModel):
    """The WHOLE settings row, not a patch.

    The panel edits these fields behind one Save bar, so the request that Save
    sends IS the new state — which also means a field the client omits is a client bug rather than
    "leave it alone", and pydantic says so with a 422. Bounds are plausibility bounds, not policy:
    they exist so a typo cannot arm something absurd, and the admin stays free inside them.
    """

    enabled: bool
    send_time: time
    timezone: str = Field(min_length=1, max_length=64)
    visibility_scope: DigestVisibilityScope
    start_date: date
    grace_minutes: int = Field(ge=0, le=1440)
    # Floor at 1 MiB (below it no digest with a single photo could ever be built) and ceiling at
    # 100 MB, comfortably above what any mail provider accepts.
    max_email_bytes: int = Field(ge=1048576, le=104857600)
    max_videos_per_memo: int = Field(ge=0, le=50)
    max_memos_per_run: int = Field(ge=1, le=1000)
    mail_title: str = Field(min_length=1, max_length=255)
    from_name: str = Field(min_length=1, max_length=255)
    # Empty is meaningful and is the seeded value: "bind to SMTP_USERNAME at send time", i.e. send
    # as whatever address actually authenticates. Anything non-empty must be an address.
    from_email: str = Field(max_length=MAX_EMAIL_LENGTH)
    hosting_enabled: bool
    dropbox_folder: str = Field(min_length=1, max_length=255)
    retention_days: int = Field(ge=1, le=3650)
    link_images_full: bool

    @field_validator("timezone")
    @classmethod
    def _loadable_iana_zone(cls, value: str) -> str:
        """The zone must be one `ZoneInfo` can actually LOAD, not merely a string of the right
        length.

        Enforced here because this is the only writer: `digest_due` converts UTC to this zone to
        decide whether today's run is due, and a zone it cannot resolve makes the digest
        permanently not-due. The gate defends against a value already in the row (it answers
        not-due and logs once), but a defence is not a contract — 422 at the door is what keeps
        an unusable zone from ever being persisted in the first place.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{value!r} is not a loadable IANA time zone") from exc
        return value

    @field_validator("from_email")
    @classmethod
    def _address_or_empty(cls, value: str) -> str:
        if value == "":
            return value
        normalised = normalise_email(value)
        if normalised is None:
            raise ValueError("from_email must be an e-mail address, or empty to use SMTP_USERNAME")
        return normalised


class DigestRecipientOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    enabled: bool
    added_by: str | None
    added_at: Rfc3339Datetime


class DigestRecipientCreate(BaseModel):
    email: str = Field(min_length=3, max_length=MAX_EMAIL_LENGTH)
    display_name: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class DigestRecipientPatch(BaseModel):
    """Partial by design — the panel toggles `enabled` on its own, without resending the name.

    An omitted field is "leave it alone" and is told apart from an explicit `null` (which clears
    the display name) by whether the key is present at all, not by the value.
    """

    # Bounded like `DigestRecipientCreate.display_name`, and for a reason a request schema must
    # carry rather than leave to the column: without it a 256-character name passes validation and
    # dies at the VARCHAR(255) on COMMIT, which the caller sees as a 500 for what is plainly a 422.
    display_name: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class DigestRunOut(BaseModel):
    id: int
    trigger: str
    started_at: Rfc3339Datetime
    finished_at: Rfc3339Datetime | None
    # NULL means "in flight". Published as a plain nullable string rather than a nullable enum ref
    # on purpose: `anyOf: [$ref, {type: null}]` is the one shape swift-openapi-generator drops
    # outright (MEMNG-105), and openapi.json is vendored whole into the iOS repo.
    status: str | None
    memos_sent: int
    parts: int
    parts_sent: int
    parts_claimed: int
    recipients: int
    error: str | None
    local_date: date


class DigestRunList(BaseModel):
    """One page of runs, in the shape every unbounded list in this API uses (`MemoList`): the
    items, the token for the page after, and a total the caller has to ask for."""

    items: list[DigestRunOut]
    nextCursor: str | None
    # null unless the caller asks (?with_total=true); the COUNT is expensive and the Runs table
    # does not show one.
    totalCount: int | None


class DigestRunClaimOut(BaseModel):
    """The answer to `POST /run`: a run row exists and nothing has executed it yet.

    Three fields and no aggregates, because at this instant there are none to report — the row is
    the CLAIM, and everything else about it is read back from
    `GET /api/admin/digest/runs/{run_id}` (which the response `Location` header names).

    `status` is always null here: NULL means "in flight", and a run that already had an outcome
    would not be something this endpoint could have just opened.
    """

    id: int
    status: str | None
    trigger: str


class DigestHostingOut(BaseModel):
    """Whether media hosting can work at all, as the panel's status line reads it.

    Two independent facts, and the difference between them is the whole value of the endpoint:

    * `configured` is the PRESENCE of the three `DROPBOX_*` env keys — never their value, which is
      secret and is echoed nowhere. It answers "could this work?" without touching the network.
    * `account` is who those keys actually authenticate as, read live from Dropbox. It is the only
      thing that distinguishes keys that are set from keys that WORK — a rotated refresh token
      leaves `configured` true forever while every upload silently falls back to inline-only.

    `account` is null whenever that answer cannot be had — unconfigured, refused, unreachable —
    because an admin status line must never be the reason a page 500s.
    """

    configured: bool
    # Required and nullable rather than optional: a generated client must always be handed the
    # field, so "the lookup could not answer" is a null it can render rather than a key it has to
    # guess the absence of.
    account: str | None


# --- helpers ----------------------------------------------------------------------------------


async def _settings_row(db: AsyncSession, *, for_update: bool = False) -> DigestSettings:
    """`domain.settings_row`, with its one domain failure translated into a status code.

    The translation is the ONLY thing this adds, and it is the reason the lookup itself lives in
    the domain module: the import script needs the same read from a process that serves no HTTP,
    and a `SettingsMissing` it has to catch is honest where an `HTTPException` was not. A missing
    singleton is a broken install rather than a caller error — the migration seeds it, and creating
    one here would be the INSERT path that makes a second row possible.
    """
    try:
        return await digest_domain.settings_row(db, for_update=for_update)
    except digest_domain.SettingsMissing as exc:
        raise HTTPException(status_code=500, detail="digest settings row is missing") from exc


def _settings_out(row: DigestSettings) -> DigestSettingsOut:
    return DigestSettingsOut(
        enabled=row.enabled,
        send_time=row.send_time,
        timezone=row.timezone,
        visibility_scope=DigestVisibilityScope(row.visibility_scope),
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
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _recipient_out(row: DigestRecipient) -> DigestRecipientOut:
    return DigestRecipientOut(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        enabled=row.enabled,
        added_by=row.added_by,
        added_at=row.added_at,
    )


async def _refuse_leaving_nobody_to_send_to(db: AsyncSession, target: DigestRecipient) -> None:
    """Refuse a change that would leave an ARMED digest with zero enabled recipients.

    Without this the panel can quietly produce a digest that is switched on and fails every
    morning with nothing to send to — the silent-failure class this whole feature exists to end.
    Disabling the digest first is the supported path, and the refusal says so.

    422 rather than 409: it is the same family as the settings guard that refuses to arm a digest
    with no recipients, and the two must not answer the same mistake with two different codes.

    **The count below is a check-then-act, so it is serialized rather than merely careful.** A
    disabled button is never the guard: two overlapping disables of the last TWO enabled recipients
    would each count the other as still enabled, both pass, and the digest would be left armed with
    nobody to send to — the exact state this refusal exists to prevent. So every recipient mutation
    that can lower the enabled count takes a row lock on the `digest_settings` singleton FIRST and
    holds it to COMMIT. The singleton is the natural mutex: there is exactly one row, every such
    mutation is already interested in it (it is what says whether the digest is armed), and one row
    taken in one order by one code path cannot deadlock. The second request therefore does not read
    a stale count — it waits, and then counts the first one's committed effect.

    The lock is taken before the early returns on purpose: a request that turns out not to need the
    guard has still declared its interest, and the alternative (decide, then lock) is the
    check-then-act this exists to close.
    """
    settings_row = await _settings_row(db, for_update=True)
    if not target.enabled:
        return  # already disabled — removing it cannot lower the enabled count
    if not settings_row.enabled:
        return  # the digest is off; leaving it with no recipients breaks no delivery
    others = await db.scalar(
        select(func.count())
        .select_from(DigestRecipient)
        .where(DigestRecipient.enabled.is_(True), DigestRecipient.id != target.id)
    )
    if not others:
        raise HTTPException(
            status_code=422,
            detail=(
                "this is the last enabled recipient of an enabled digest — disable the digest "
                "first, or enable another recipient"
            ),
        )


async def _refuse_arming_with_no_recipients(db: AsyncSession) -> None:
    """Refuse to arm a digest that has nobody to send to.

    An armed digest with zero enabled recipients writes a `failed` run every morning and can never
    do anything else — the silent-failure class this whole feature exists to end, arrived at from
    the settings side. 422, the same code and the same family as
    `_refuse_leaving_nobody_to_send_to`, because it is the same mistake approached from the other
    side and answering it two different ways would be a worse API than either answer.

    The caller holds the `digest_settings` row lock, which is what makes this count trustworthy.
    """
    enabled = await db.scalar(
        select(func.count()).select_from(DigestRecipient).where(DigestRecipient.enabled.is_(True))
    )
    if not enabled:
        raise HTTPException(
            status_code=422,
            detail=(
                "the digest cannot be enabled with no enabled recipients — add one first, or "
                "leave it disabled"
            ),
        )


# --- settings ---------------------------------------------------------------------------------


@router.get("/settings", response_model=DigestSettingsOut)
async def get_digest_settings(
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestSettingsOut:
    return _settings_out(await _settings_row(db))


@router.put("/settings", response_model=DigestSettingsOut)
async def put_digest_settings(
    body: DigestSettingsPut,
    admin: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestSettingsOut:
    """Update the one settings row. There is no create path here, deliberately: the database allows
    exactly one row and this router only ever UPDATEs it, so a second one is impossible.

    The row is taken FOR UPDATE for the whole request, not because a fresh read is needed but
    because arming is a check-then-act: two overlapping requests, one arming the digest and one
    disabling the last recipient, would each see the other's pre-state and both pass, leaving an
    armed digest with nobody to send to. Disabling a recipient locks that same one row in the same
    order, so the two paths serialize against each other and cannot deadlock.
    """
    row = await _settings_row(db, for_update=True)
    if body.enabled:
        await _refuse_arming_with_no_recipients(db)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    row.updated_by = admin.subject
    await db.commit()
    # `updated_at` is stamped by the database (`onupdate=func.now()`), so the flush leaves that one
    # attribute expired — and reading an expired attribute on an async session outside a greenlet
    # raises rather than lazily loading. Refetch the row explicitly instead of returning a value
    # the response model cannot render.
    await db.refresh(row)
    return _settings_out(row)


# --- recipients -------------------------------------------------------------------------------


@router.get("/recipients", response_model=list[DigestRecipientOut])
async def list_digest_recipients(
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[DigestRecipientOut]:
    rows = (
        await db.execute(select(DigestRecipient).order_by(DigestRecipient.email.asc()))
    ).scalars()
    return [_recipient_out(row) for row in rows]


@router.post("/recipients", response_model=DigestRecipientOut, status_code=201)
async def add_digest_recipient(
    body: DigestRecipientCreate,
    admin: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestRecipientOut:
    email = normalise_email(body.email)
    if email is None:
        raise HTTPException(status_code=422, detail="not a valid e-mail address")
    existing = (
        await db.execute(select(DigestRecipient).where(DigestRecipient.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        # Compared against the NORMALISED form, so `A@B.com` collides with `a@b.com` — one mailbox
        # listed twice would receive the digest twice.
        raise HTTPException(status_code=409, detail="that address is already a recipient")
    row = DigestRecipient(
        email=email,
        display_name=body.display_name,
        enabled=body.enabled,
        added_by=admin.subject,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        # The check above loses to a concurrent POST of the same address; the unique index is what
        # actually decides, and the loser gets the same answer it would have got a moment earlier.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="that address is already a recipient"
        ) from exc
    return _recipient_out(row)


@router.patch("/recipients/{recipient_id}", response_model=DigestRecipientOut)
async def update_digest_recipient(
    recipient_id: int,
    body: DigestRecipientPatch,
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestRecipientOut:
    row = (
        await db.execute(select(DigestRecipient).where(DigestRecipient.id == recipient_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="recipient not found")
    if "display_name" in body.model_fields_set:
        row.display_name = body.display_name
    if body.enabled is False:
        await _refuse_leaving_nobody_to_send_to(db, row)
    if body.enabled is not None:
        row.enabled = body.enabled
    await db.commit()
    return _recipient_out(row)


@router.delete("/recipients/{recipient_id}", status_code=204)
async def remove_digest_recipient(
    recipient_id: int,
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    row = (
        await db.execute(select(DigestRecipient).where(DigestRecipient.id == recipient_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="recipient not found")
    await _refuse_leaving_nobody_to_send_to(db, row)
    await db.delete(row)
    await db.commit()


# --- runs and hosting -------------------------------------------------------------------------


def _run_out(row: DigestRun) -> DigestRunOut:
    return DigestRunOut(
        id=row.id,
        trigger=row.trigger,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        memos_sent=row.memos_sent,
        parts=row.parts,
        parts_sent=row.parts_sent,
        parts_claimed=row.parts_claimed,
        recipients=row.recipients,
        error=row.error,
        local_date=row.local_date,
    )


def encode_run_cursor(row: DigestRun) -> str:
    """The keyset cursor of the runs list: the last row of the page, opaque and base64url like the
    memo feed's and the media library's (`app/services/cursors.py` mints all three). The key is
    `(started_at, id)` because that is the order — two runs can share a start instant, so the id
    is what makes the position unambiguous."""
    return encode_cursor_payload(f"{row.started_at.isoformat()}|{row.id}")


def decode_run_cursor(cursor: str) -> tuple[datetime, int]:
    """`InvalidCursor` (→ 422) for anything this server did not mint — never a 500, and never a
    silent restart from the top of the list, which would make a paging client loop for ever over
    the first page."""
    raw = decode_cursor_payload(cursor)
    stamp, _, ident = raw.rpartition("|")
    if not stamp or not ident:
        raise InvalidCursor("cursor is not a keyset token")
    try:
        return datetime.fromisoformat(stamp), int(ident)
    except ValueError:
        raise InvalidCursor("cursor is not a keyset token") from None


@router.get("/runs", response_model=DigestRunList)
async def list_digest_runs(
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    with_total: bool = False,
) -> DigestRunList:
    """The runs, newest first — the panel's Runs table, paged by keyset.

    Unbounded lists in this API page by cursor rather than by a bare `limit`, and this one is
    unbounded: a run row is written every day for ever, so a caller that can only ask for "the
    first N" can never reach the rest of the history. When more rows follow, `nextCursor` is
    non-null; resend it as `?cursor=` for the page after.

    Ordered by `started_at` DESC and then `id` DESC: two runs can share a start instant (an
    imported row is stamped with the same instant as anything else committed beside it), and an
    unstable order would make the table shuffle between refreshes — and would make a keyset cursor
    skip or repeat rows, which is worse than shuffling.

    `totalCount` is null unless `?with_total=true`, the same opt-in the memo feed uses: a COUNT is
    the most expensive part of a page and the table that reads this does not show one.
    """
    conditions = []
    if cursor is not None:
        # `is not None`, not truthiness: `?cursor=` sent empty is a BAD token, not "start over" —
        # falling through to page 1 is the loop-for-ever failure the strict decoder exists to end.
        started_before, before_id = decode_run_cursor(cursor)
        conditions.append(
            or_(
                DigestRun.started_at < started_before,
                and_(DigestRun.started_at == started_before, DigestRun.id < before_id),
            )
        )
    total: int | None = None
    if with_total:
        # The WHOLE list, never the tail after the cursor — `totalCount` answers "how many runs are
        # there?", which is a property of the collection and not of the page. Applying the keyset
        # predicate here would make the number shrink with every page turned, and a client showing
        # it beside a paginator would count down towards zero as the user walked forward.
        total = await db.scalar(select(func.count()).select_from(DigestRun))
    rows = list(
        (
            await db.execute(
                select(DigestRun)
                .where(*conditions)
                .order_by(DigestRun.started_at.desc(), DigestRun.id.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    next_cursor = encode_run_cursor(rows[limit - 1]) if len(rows) > limit else None
    return DigestRunList(
        items=[_run_out(row) for row in rows[:limit]],
        nextCursor=next_cursor,
        totalCount=total,
    )


@router.get("/runs/{run_id}", response_model=DigestRunOut)
async def get_digest_run(
    run_id: int,
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestRunOut:
    """One run by id — what the `Location` of a 202 from `POST /run` points at.

    A claim is only useful if its progress can be read back, and paging the whole list to find one
    row is not that.
    """
    row = await db.get(DigestRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_out(row)


@router.get("/hosting", response_model=DigestHostingOut)
async def get_digest_hosting(
    _: Annotated[User, Depends(auth.require_admin)],
) -> DigestHostingOut:
    """Report whether Dropbox hosting is configured, and who the keys authenticate as.

    The account lookup is made once per request and only when the keys are present — there is no
    process-wide cache, deliberately: a cached identity would keep reporting a working channel
    after a token was revoked, which is the exact failure this line exists to expose. It is
    read-only (`/2/users/get_current_account`) and answers None on any failure, so a slow or angry
    Dropbox costs the panel a null, not an error.
    """
    configured = digest_domain.dropbox_configured()
    account = (
        await digest_hosting.current_account(
            app_key=app_settings.DROPBOX_APP_KEY,
            app_secret=app_settings.DROPBOX_APP_SECRET,
            refresh_token=app_settings.DROPBOX_REFRESH_TOKEN,
        )
        if configured
        else None
    )
    return DigestHostingOut(configured=configured, account=account)


# --- actions: preview, test-send, run now ------------------------------------------------------


class DigestPreviewMemo(BaseModel):
    uid: str
    memo_id: int
    created_at: Rfc3339Datetime
    # The LOCAL day this memo groups under in the mail, which is what the reader sees — not the
    # UTC date of `created_at`, which differs for anything posted late in the evening.
    local_day: date


class DigestDeferralOut(BaseModel):
    uid: str
    reason: str


class DigestPreviewOut(BaseModel):
    """What a run would send right now, rendered but persisted nowhere.

    `html`/`plain` are PART ONE. A preview that concatenated every part would misrepresent the
    thing it is previewing — the household receives separate messages — and `parts` says how many
    there would be.
    """

    html: str
    plain: str
    memo_count: int
    parts: int
    selected: list[DigestPreviewMemo]
    deferred: list[DigestDeferralOut]


class DigestTestSendIn(BaseModel):
    """`{"empty": true}` renders the "no memos selected" body directly, without consulting the
    selection at all — the pure SMTP probe, and one that writes no settings anywhere in it."""

    empty: bool = False


class DigestTestSendOut(BaseModel):
    sent_to: str
    memo_count: int


@router.get("/preview", response_model=DigestPreviewOut)
async def preview_digest(
    _: Annotated[User, Depends(auth.require_admin)],
) -> DigestPreviewOut:
    """Render what would go out now. Persists nothing and performs NO Dropbox call.

    `selected` NAMES the memos rather than counting them: a preview whose answer is a number
    cannot be checked against what a run then actually mails.

    No request-scoped session is taken. The render walks the filesystem for every card's media,
    and the reads it needs are taken in short sessions that close before that walk starts — a
    request session would be an idle transaction held across all of it.
    """
    rendered, settings_row = await digest_runner.preview()
    if rendered.parts:
        payload = digest_runner.part_payload(rendered, 1, settings_row)
        html, plain = payload[1], payload[2]
    else:
        from app.services.digest import render as digest_render

        html, plain = digest_render.render_empty(settings_row.mail_title)
    return DigestPreviewOut(
        html=html,
        plain=plain,
        memo_count=len(rendered.selected),
        parts=len(rendered.parts),
        selected=[
            DigestPreviewMemo(
                uid=candidate.uid,
                memo_id=candidate.memo_id,
                created_at=candidate.created_at,
                local_day=local_date_of(candidate.created_at, settings_row.timezone),
            )
            for candidate in rendered.selected
        ],
        deferred=[
            DigestDeferralOut(uid=item.uid, reason=item.reason) for item in rendered.deferred
        ],
    )


@router.post("/test-send", response_model=DigestTestSendOut)
async def test_send_digest(
    body: DigestTestSendIn,
    admin: Annotated[User, Depends(auth.require_admin)],
) -> DigestTestSendOut:
    """Send ONE real mail to the calling admin's own address, and persist nothing.

    The destination comes from the authenticated session, never from a request field: an endpoint
    that took an address would be an open relay for anyone who reached it, and it would let a probe
    mail the configured recipients. It performs no Dropbox upload either — it renders with whatever
    hosted links already exist, and a video with none carries the "not hosted" chip.

    It stays SYNCHRONOUS, unlike a run, and the reason is that it is bounded by construction: no
    uploads, one recipient, one message. Its database reads still happen in short sessions that
    close before SMTP is contacted, which is why it takes no request session of its own.
    """
    if not admin.email:
        raise HTTPException(
            status_code=422,
            detail="your account has no e-mail address, so there is nowhere to send the test",
        )
    if not digest_domain.smtp_configured():
        raise HTTPException(status_code=422, detail=digest_runner.ERROR_SMTP_UNCONFIGURED)
    try:
        memo_count = await digest_runner.test_send(address=admin.email, empty=body.empty)
    except digest_runner.SenderIsRecipient as exc:
        # The same refusal a real run applies. Answered 422 rather than sent-and-hoped: iCloud
        # drops From == To with attachments, so the probe would report success and deliver
        # nothing — and a probe that lies is worse than no probe.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await digest_metrics.refresh_now()
    logger.info("digest.test_send", subject=admin.subject, memos=memo_count)
    return DigestTestSendOut(sent_to=admin.email, memo_count=memo_count)


def _location_header() -> dict[str, Any]:
    """A fresh dict per call: FastAPI merges `openapi_extra` into the operation by reference, so a
    shared constant would leave two operations aliasing the same node in the served document."""
    return {
        "Location": {
            "description": (
                "The claimed run: `GET /api/admin/digest/runs/{run_id}`. Poll it for the run's "
                "progress and its terminal status — the work happens in the worker, not in this "
                "request."
            ),
            "schema": {"type": "string"},
        }
    }


@router.post(
    "/run",
    response_model=DigestRunClaimOut,
    status_code=202,
    openapi_extra={"responses": {"202": {"headers": _location_header()}}},
)
async def run_digest_now(
    admin: Annotated[User, Depends(auth.require_admin)],
    response: Response,
) -> DigestRunClaimOut:
    """CLAIM a manual run and answer 202. The worker executes it.

    This endpoint commits exactly one thing — the run row, which IS the single-flight lock — and
    returns. It does not send, upload or render, and that split is the point: a run can
    legitimately last hours (up to `max_memos_per_run` memos, several videos each, Dropbox
    upload-session operations that allow 600 s EACH), and a contract that awaited it would tie the
    household's mail to a browser tab, a proxy read timeout and a reload. The scheduled run is
    already backgrounded for exactly this reason; a manual one is not different work, only
    differently triggered.

    A manual run sits OUTSIDE the scheduled-day index by design, so it is always possible on a day
    that already had one. Its 409 comes from the insert that loses the race for the single-flight
    index — never from a pre-flight "is anything running?" read, which would be exactly the TOCTOU
    race that index exists to close and which two concurrent callers would both pass.

    `Location` names the run, and `GET /api/admin/digest/runs/{run_id}` is where its progress
    and its outcome are read back. Until the worker adopts the row it stays `status: null` with no
    heartbeat; if no worker ever does, the stale-run sweep closes it as `failed` rather than
    leaving it in flight for ever.
    """
    async with async_session() as db:
        settings = await digest_runner.load_settings(db)
        try:
            run_id = await digest_runner.open_run(
                db,
                trigger=DigestTrigger.manual,
                local_date=local_date_of(datetime.now(UTC), settings.timezone),
            )
        except digest_runner.RunSkipped as exc:
            raise HTTPException(
                status_code=409, detail="a digest run is already in flight"
            ) from exc
    response.headers["Location"] = f"{router.prefix}/runs/{run_id}"
    logger.info("digest.manual_run_claimed", subject=admin.subject, run_id=run_id)
    return DigestRunClaimOut(id=run_id, status=None, trigger=DigestTrigger.manual)
