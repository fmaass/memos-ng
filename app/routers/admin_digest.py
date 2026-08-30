"""The admin-only API over the daily digest's state (MEMNG-233).

Everything here sits behind `require_admin`, which is built on `require_session_user` — so an
access token never reaches these routes (the bearer is refused before the cookie is read) and a
non-admin session is answered 403. That is the whole authorization story for the feature: the
Mail panel hides itself for a non-admin as a courtesy, but the refusal happens here, at the sink.

Phase 2 adds the three ACTION endpoints the panel also needs — preview, test-send and run-now.
Each of them renders or sends, so each sits behind the same `require_admin` as everything above,
and each is deliberately explicit about what it persists: preview and test-send persist NOTHING
(not a run row, not a `digest_hosted_media` row, not a Dropbox file), while run-now performs a real
run through the very same `runner.py` the worker uses.
"""

import re
from datetime import date, time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.config import settings as app_settings
from app.database import get_session
from app.models import (
    DigestRecipient,
    DigestRun,
    DigestSettings,
    DigestTrigger,
    DigestVisibilityScope,
    User,
)
from app.services.digest import metrics as digest_metrics
from app.services.digest import runner as digest_runner
from app.services.digest.gate import local_date_of

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin/digest", tags=["admin-digest"])

# The settings table holds exactly one row, and this is its id. `CHECK (id = 1)` in the migration
# makes a second row impossible at the database; every handler below reads and UPDATEs this row and
# none of them INSERTs, which is the other half of that guarantee.
SETTINGS_ID = 1


# --- e-mail normalisation ---------------------------------------------------------------------

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
# RFC 5321's addr-spec ceiling, and the width of the column.
MAX_EMAIL_LENGTH = 320


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


class DigestSettingsPut(BaseModel):
    """The WHOLE settings row, not a patch.

    The panel edits these fields behind one Save bar (design 3285 §6), so the request that Save
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
    the display name) by `model_fields_set`, not by the value.
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


class DigestHostingOut(BaseModel):
    """Whether media hosting can work at all, as the panel's status line reads it.

    Phase 1 answers from the PRESENCE of the three `DROPBOX_*` env keys and nothing else — no
    network call is made and `account` is therefore always null here. The live account lookup that
    fills it in arrives with the hosting service in Phase 2; a caller reading `configured` alone
    gets the same answer either way, which is what the "Not configured — mails go out inline-only"
    line needs.
    """

    configured: bool
    account: str | None = None


# --- helpers ----------------------------------------------------------------------------------


async def _settings_row(db: AsyncSession, *, for_update: bool = False) -> DigestSettings:
    """The singleton, optionally LOCKED for the rest of this transaction.

    `for_update=True` is not about reading a fresh value — it is a mutex. See
    `_refuse_leaving_nobody_to_send_to`, the only caller that asks for it.
    """
    statement = select(DigestSettings).where(DigestSettings.id == SETTINGS_ID)
    if for_update:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        # The migration seeds it, so its absence is a broken install rather than a caller error —
        # and creating one here would be the INSERT path that makes a second row possible.
        raise HTTPException(status_code=500, detail="digest settings row is missing")
    return row


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
    """Refuse a change that would leave an ARMED digest with zero enabled recipients (A2).

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
    """Refuse to arm a digest that has nobody to send to (Phase 2 guard).

    Phase 1 deliberately allowed it so the panel could be built against a contract that did not yet
    exist; the pipeline is what makes it a real failure — an armed digest with zero enabled
    recipients writes a `failed` run every morning and can never do anything else. 422, the same
    code and the same family as `_refuse_leaving_nobody_to_send_to`, because it is the same mistake
    approached from the other side and answering it two different ways would be a worse API than
    either answer.

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
    """Update the one settings row. There is no create path here, deliberately — see SETTINGS_ID.

    The row is taken FOR UPDATE for the whole request, not because a fresh read is needed but
    because arming is a check-then-act: two overlapping requests, one arming the digest and one
    disabling the last recipient, would each see the other's pre-state and both pass, leaving an
    armed digest with nobody to send to. `_refuse_leaving_nobody_to_send_to` locks the same one row
    in the same order, so the two paths serialize against each other and cannot deadlock.
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


@router.get("/runs", response_model=list[DigestRunOut])
async def list_digest_runs(
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[DigestRunOut]:
    """The most recent runs, newest first — the panel's Runs table.

    Ordered by `started_at` DESC and then `id` DESC: two runs can share a start instant (the import
    row is stamped by the same `now()` as anything else committed in that transaction), and an
    unstable order would make the table shuffle between refreshes.
    """
    rows = (
        await db.execute(
            select(DigestRun)
            .order_by(DigestRun.started_at.desc(), DigestRun.id.desc())
            .limit(limit)
        )
    ).scalars()
    return [
        DigestRunOut(
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
        for row in rows
    ]


@router.get("/hosting", response_model=DigestHostingOut)
async def get_digest_hosting(
    _: Annotated[User, Depends(auth.require_admin)],
) -> DigestHostingOut:
    """Report whether Dropbox hosting is configured, from the env keys alone.

    Presence, never the value: the three keys are secrets and nothing about them is echoed back —
    `configured` is the whole answer. All three are required, because two out of three cannot
    refresh a token, and an empty key is the switch that disables hosting globally.
    """
    configured = all(
        (
            app_settings.DROPBOX_APP_KEY,
            app_settings.DROPBOX_APP_SECRET,
            app_settings.DROPBOX_REFRESH_TOKEN,
        )
    )
    return DigestHostingOut(configured=configured)


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
    selection at all — the pure SMTP probe, with no settings write anywhere in it (R3)."""

    empty: bool = False


class DigestTestSendOut(BaseModel):
    sent_to: str
    memo_count: int


async def _run_out(run_id: int) -> DigestRunOut:
    """Read one run row back in its own short session."""
    from app.database import async_session

    async with async_session() as db:
        row = await db.get(DigestRun, run_id)
        if row is None:  # pragma: no cover — the row was committed a moment ago
            raise HTTPException(status_code=500, detail="the run row vanished")
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


@router.get("/preview", response_model=DigestPreviewOut)
async def preview_digest(
    _: Annotated[User, Depends(auth.require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestPreviewOut:
    """Render what would go out now. Persists nothing and performs NO Dropbox call.

    `selected` is the list MEMNG-236 AC2 and the Phase 5 one-victim read-back are checked against,
    so it names the memos rather than counting them.
    """
    rendered, settings_row = await digest_runner.preview(db)
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
    db: Annotated[AsyncSession, Depends(get_session)],
) -> DigestTestSendOut:
    """Send ONE real mail to the calling admin's own address, and persist nothing.

    The destination comes from the authenticated session, never from a request field: an endpoint
    that took an address would be an open relay for anyone who reached it, and it would let a probe
    reach `digest_recipients`. It performs no Dropbox upload either (A6) — it renders with whatever
    hosted links already exist, and a video with none carries the "not hosted" chip.
    """
    if not admin.email:
        raise HTTPException(
            status_code=422,
            detail="your account has no e-mail address, so there is nowhere to send the test",
        )
    if not digest_runner.smtp_configured():
        raise HTTPException(status_code=422, detail=digest_runner.ERROR_SMTP_UNCONFIGURED)
    memo_count = await digest_runner.test_send(db, address=admin.email, empty=body.empty)
    await digest_metrics.refresh_now()
    logger.info("digest.test_send", subject=admin.subject, memos=memo_count)
    return DigestTestSendOut(sent_to=admin.email, memo_count=memo_count)


@router.post("/run", response_model=DigestRunOut)
async def run_digest_now(
    admin: Annotated[User, Depends(auth.require_admin)],
) -> DigestRunOut:
    """Perform a real run through the same `runner.py` the worker uses, with `trigger = 'manual'`.

    A manual run sits OUTSIDE the scheduled-day index by design, so it is always possible on a day
    that already had one. Its 409 is derived from the INSERT conflicting with
    `uq_digest_runs_single_inflight` — never from a pre-flight "is anything running?" read, which
    would be exactly the TOCTOU race that index exists to close and which two concurrent callers
    would both pass.

    No request-scoped session is taken: the run opens and commits its own, repeatedly, and holding
    a pooled connection idle for the minutes a real run takes would be pressure on a pool the app
    deliberately caps at ten (ADR-0009).
    """
    from datetime import UTC, datetime

    from app.database import async_session

    async with async_session() as db:
        settings_row = await digest_runner.load_settings(db)
        host_factory = digest_runner.make_dropbox_host_factory(settings_row, datetime.now(UTC))
    try:
        result = await digest_runner.run_digest(
            trigger=DigestTrigger.manual, host_factory=host_factory
        )
    except digest_runner.RunSkipped as exc:
        raise HTTPException(
            status_code=409, detail="a digest run is already in flight"
        ) from exc
    await digest_metrics.refresh_now()
    logger.info(
        "digest.manual_run",
        subject=admin.subject,
        run_id=None if result is None else result.run_id,
        status=None if result is None else result.status,
    )
    if result is None:  # pragma: no cover — the claim was lost to a sweep mid-run
        raise HTTPException(
            status_code=409, detail="the run lost its claim and was abandoned before sending"
        )
    return await _run_out(result.run_id)
