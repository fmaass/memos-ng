"""One digest run: the row that IS the lock, the claim CAS, the per-part commit, and the sweeps.

Three rules run through everything here and each was bought by a named failure mode.

**The run row is the single-flight lock.** It is INSERTed and COMMITted before any other work,
under two unique partial indexes (`uq_digest_runs_scheduled_local_date`,
`uq_digest_runs_single_inflight`). A committed row is durable and self-fencing: no crash, reap or
connection death releases it, so there is nothing to fence — unlike a session-scoped advisory
lock, which dies with its connection and would release while a runner sits between its liveness
check and its SMTP call. No path here decides single-flight by reading before writing; the
concurrent loser learns from a unique violation.

**Every claim, every progress update and the terminal write carry `AND status IS NULL` in the
SQL.** The ORM validator on `DigestRun.status` guards one session's object and knows nothing about
the stale-run sweep committing from another connection, so it cannot be the guard. The predicate
is: a run the sweep has already closed cannot be advanced, and its still-alive task stops at its
very next claim — BEFORE any SMTP contact — rather than continuing under a row that now belongs to
nobody.

**Delivery is committed per ACCEPTED part** (user decision 1). Each part's `digest_deliveries`
rows land in their own transaction the moment that part's SMTP send is accepted by at least one
envelope recipient. That makes the digest at-least-once per part, not at-most-once, and that is
the chosen policy: duplicate over loss. The window is a crash between SMTP acceptance and that
part's commit — at most one part per attempt, repeated on every attempt for as long as a crash
recurs at exactly that point. It is alarmed rather than silent: the crashed run leaves a
`status IS NULL` row the sweep marks `failed`, so `memosng_digest_last_success_timestamp_seconds`
never advances and the 26 h stale alert fires.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestSettings, DigestTrigger
from app.services.digest import hosting, render
from app.services.digest import select as selection_mod
from app.services.digest.gate import FALLBACK_TIMEZONE, local_date_of, resolve_zone
from app.services.digest.hosting import DropboxHost, MediaHost, MediaLinks
from app.services.digest.mailer import (
    MailTransport,
    SmtplibTransport,
    SmtpSettings,
    accepted_count,
    build_message,
    refusal_note,
)
from app.services.digest.render import DayGroup, InlineImage
from app.services.digest.select import Candidate, Deferral
from app.services.digest.splitting import Part, build_cid_to_bytes, split_into_parts

logger = structlog.get_logger(__name__)

SENT = "sent"
NOTHING_TO_SEND = "nothing_to_send"
DEFERRED = "deferred"
FAILED = "failed"

# Fifteen minutes of SILENCE, not of elapsed time. A healthy run stamps `heartbeat_at` before and
# after every part send and after every Dropbox upload-session operation, so the longest gap it
# can leave is one operation (≤ 10 min at the 600 s per-operation timeout) — comfortably below
# this bar, and unrelated to how long the whole run takes.
STALE_RUN_MINUTES = 15

ERROR_SMTP_UNCONFIGURED = "smtp not configured"
ERROR_NO_RECIPIENTS = "no enabled recipients"
ERROR_SENDER_IS_RECIPIENT = "the sender address is also a recipient"
ERROR_RUNNER_DIED = "runner died"

SessionFactory = async_sessionmaker[AsyncSession]
HostFactory = Callable[[hosting.Operation], Awaitable[MediaHost | None]]


class RunSkipped(RuntimeError):
    """Another run already owns this slot — the INSERT lost to a unique partial index."""


class RunLost(RuntimeError):
    """A fenced CAS updated 0 rows: the sweep closed this run, or another actor advanced it."""


@dataclass
class RunResult:
    run_id: int
    status: str
    parts: int
    parts_sent: int
    memos_sent: int
    error: str | None = None


@dataclass
class RenderedDigest:
    """Everything a set of parts needs, built once and shared by preview, test-send and a run."""

    day_groups: list[DayGroup] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    inline_by_cid: dict[str, InlineImage] = field(default_factory=dict)
    date_range: str = ""
    selected: list[Candidate] = field(default_factory=list)
    deferred: list[Deferral] = field(default_factory=list)
    links: MediaLinks = field(default_factory=MediaLinks)


# --- the run row, and every fenced write against it ---------------------------------------------


async def open_run(db: AsyncSession, *, trigger: str, local_date: date) -> int:
    """INSERT and COMMIT the run row before any other work. Raises `RunSkipped` on a conflict.

    The conflict IS the answer, never a pre-flight "is anything running?" read: a check-then-insert
    is exactly the TOCTOU race the two unique partial indexes exist to close, and two concurrent
    callers would both pass it.
    """
    try:
        run_id = await db.scalar(
            sa_text(
                "INSERT INTO digest_runs (trigger, local_date) VALUES (:trigger, :local_date) "
                "RETURNING id"
            ),
            {"trigger": trigger, "local_date": local_date},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise RunSkipped(str(exc.orig)) from exc
    assert run_id is not None
    return int(run_id)


async def heartbeat(db: AsyncSession, run_id: int) -> bool:
    """Stamp liveness, fenced. False means the run is no longer in flight."""
    stamped = await db.scalar(
        sa_text(
            "UPDATE digest_runs SET heartbeat_at = now() "
            "WHERE id = :id AND status IS NULL RETURNING id"
        ),
        {"id": run_id},
    )
    await db.commit()
    return stamped is not None


async def claim_part(db: AsyncSession, run_id: int, part_index: int) -> bool:
    """Claim part `part_index` BEFORE it touches SMTP. False means: do not send.

    Zero rows updated means either another actor advanced this run or the sweep has already
    declared it dead. Both answers are the same one — stop, without sending — and the
    `AND status IS NULL` predicate is what makes the second case safe: a swept-but-still-alive
    task halts here rather than mailing under a row that now belongs to nobody.
    """
    claimed = await db.scalar(
        sa_text(
            "UPDATE digest_runs SET parts_claimed = parts_claimed + 1, heartbeat_at = now() "
            "WHERE id = :id AND parts_claimed = :previous AND status IS NULL RETURNING id"
        ),
        {"id": run_id, "previous": part_index - 1},
    )
    await db.commit()
    return claimed is not None


async def record_progress(
    db: AsyncSession, run_id: int, *, parts: int, recipients: int
) -> bool:
    updated = await db.scalar(
        sa_text(
            "UPDATE digest_runs SET parts = :parts, recipients = :recipients, "
            "heartbeat_at = now() WHERE id = :id AND status IS NULL RETURNING id"
        ),
        {"id": run_id, "parts": parts, "recipients": recipients},
    )
    await db.commit()
    return updated is not None


async def commit_part(
    db: AsyncSession,
    run_id: int,
    *,
    part_index: int,
    memo_ids: list[int],
    incomplete_ids: set[int],
) -> None:
    """This part's deliveries and its `parts_sent` advance, in ONE transaction.

    The fenced advance goes FIRST: if it updates no row the run is not ours any more, and the
    deliveries must not be written — so the transaction is rolled back before a single row lands.
    """
    advanced = await db.scalar(
        sa_text(
            "UPDATE digest_runs SET parts_sent = parts_sent + 1, heartbeat_at = now() "
            "WHERE id = :id AND parts_sent = :previous AND status IS NULL RETURNING id"
        ),
        {"id": run_id, "previous": part_index - 1},
    )
    if advanced is None:
        await db.rollback()
        raise RunLost(f"run {run_id} is no longer in flight at part {part_index}")
    for memo_id in memo_ids:
        await db.execute(
            sa_text(
                "INSERT INTO digest_deliveries (memo_id, run_id, part_index, incomplete) "
                "VALUES (:memo_id, :run_id, :part_index, :incomplete) "
                "ON CONFLICT (memo_id) DO NOTHING"
            ),
            {
                "memo_id": memo_id,
                "run_id": run_id,
                "part_index": part_index,
                "incomplete": memo_id in incomplete_ids,
            },
        )
    await db.commit()


async def finish_run(
    db: AsyncSession,
    run_id: int,
    *,
    status: str,
    memos_sent: int = 0,
    error: str | None = None,
) -> bool:
    """The terminal write: `status`, `finished_at` and the aggregates, once, fenced.

    `AND status IS NULL` is what keeps `memosng_digest_runs_total{status}` a Counter: a row whose
    status is already terminal — because the sweep closed it — is not rewritten, so no label is
    ever decremented and no false outcome is published.
    """
    finished = await db.scalar(
        sa_text(
            "UPDATE digest_runs SET status = :status, finished_at = now(), "
            "memos_sent = :memos_sent, error = :error, heartbeat_at = now() "
            "WHERE id = :id AND status IS NULL RETURNING id"
        ),
        {"id": run_id, "status": status, "memos_sent": memos_sent, "error": error},
    )
    await db.commit()
    if finished is None:
        # The sweep closed this run while it was still working. Its status is already terminal and
        # must not be rewritten — that is what keeps the Counter monotonic.
        logger.warning("digest.terminal_write_lost", run_id=run_id, intended_status=status)
    return finished is not None


async def sweep_stale_runs(
    session_factory: SessionFactory = async_session, *, stale_minutes: int = STALE_RUN_MINUTES
) -> int:
    """Terminate runs whose process died, in ONE atomic statement.

    The stale predicate lives in the same statement as the status write, never in a prior SELECT.
    That is a correctness requirement, not a style one: a runner that beats between a would-be
    SELECT and the UPDATE must survive, and under READ COMMITTED this statement re-evaluates its
    WHERE against the row version the heartbeat committed — so it affects zero rows and the live
    run keeps its lock. A select-then-update would have decided on the stale snapshot and killed
    it, dropping it out of `uq_digest_runs_single_inflight` while it was still sending.

    `COALESCE(heartbeat_at, started_at)` is what judges a run that has not beaten yet — every run
    predating the Phase-2 migration, and every run still in its first upload — on the same bar.
    """
    async with session_factory() as db:
        result = await db.execute(
            sa_text(
                "UPDATE digest_runs SET status = :status, error = :error, finished_at = now() "
                "WHERE status IS NULL "
                "AND COALESCE(heartbeat_at, started_at) < now() - make_interval(mins => :mins) "
                "RETURNING id"
            ),
            {"status": FAILED, "error": ERROR_RUNNER_DIED, "mins": stale_minutes},
        )
        swept = len(result.scalars().all())
        await db.commit()
    if swept:
        logger.warning("digest.stale_runs_swept", count=swept, stale_minutes=stale_minutes)
    return swept


# --- reading the state a run needs ---------------------------------------------------------------


async def load_settings(db: AsyncSession) -> DigestSettings:
    row = await db.get(DigestSettings, 1)
    if row is None:
        raise RuntimeError("digest settings row is missing — the migration seeds it")
    return row


async def enabled_recipients(db: AsyncSession) -> list[str]:
    rows = await db.execute(
        sa_text(
            "SELECT email FROM digest_recipients WHERE enabled IS TRUE ORDER BY email ASC"
        )
    )
    return [row[0] for row in rows]


async def last_ledger_local_date(db: AsyncSession) -> date | None:
    """The local date of the most recent `scheduled` or `manual` run, in flight or terminal.

    An in-flight row counts, which is what makes the gate answer false for a second worker before
    the unique index ever has to — the index guarantees it, this makes it the ordinary case.
    """
    return await db.scalar(
        sa_text(
            "SELECT max(local_date) FROM digest_runs WHERE trigger IN ('scheduled', 'manual')"
        )
    )


async def existing_hosted_links(db: AsyncSession, attachment_ids: list[int]) -> dict[int, str]:
    """Links already recorded for these attachments and not yet swept.

    A test-send and a preview render from these and upload nothing (A6): a probe must never create
    a Dropbox file the registry does not know about.
    """
    if not attachment_ids:
        return {}
    rows = await db.execute(
        sa_text(
            "SELECT attachment_id, shared_link FROM digest_hosted_media "
            "WHERE attachment_id = ANY(:ids) AND swept_at IS NULL"
        ),
        {"ids": attachment_ids},
    )
    return {row[0]: row[1] for row in rows}


async def record_hosted_media(db: AsyncSession, uploads: list[hosting.HostedUpload]) -> None:
    for upload in uploads:
        await db.execute(
            sa_text(
                "INSERT INTO digest_hosted_media "
                "(attachment_id, kind, dropbox_path, shared_link, bytes) "
                "VALUES (:attachment_id, :kind, :dropbox_path, :shared_link, :bytes) "
                "ON CONFLICT (attachment_id) DO UPDATE SET "
                "kind = EXCLUDED.kind, dropbox_path = EXCLUDED.dropbox_path, "
                "shared_link = EXCLUDED.shared_link, bytes = EXCLUDED.bytes, "
                "uploaded_at = now(), swept_at = NULL"
            ),
            {
                "attachment_id": upload.attachment_id,
                "kind": upload.kind,
                "dropbox_path": upload.dropbox_path,
                "shared_link": upload.shared_link,
                "bytes": upload.bytes,
            },
        )
    await db.commit()


# --- building the mail ---------------------------------------------------------------------------


def sender_address(settings: DigestSettings) -> str:
    """`from_email`, or the address that actually authenticates when it is empty (the seeded
    value's documented meaning)."""
    return settings.from_email or app_settings.SMTP_USERNAME


def smtp_configured() -> bool:
    return bool(app_settings.SMTP_USERNAME and app_settings.SMTP_PASSWORD)


async def build_digest(
    db: AsyncSession,
    settings: DigestSettings,
    selection: selection_mod.Selection,
    *,
    host: MediaHost | None,
    upload: bool,
) -> RenderedDigest:
    """Host, render and split an already-made selection — everything up to (not including) SMTP.

    `upload=False` is the probe contract: links are resolved from `digest_hosted_media` alone and
    no Dropbox call is made, so a preview or a test-send can never leave a hosted file behind.

    The selection is TRUNCATED at `max_memos_per_run` here, which is how a preview and a test-send
    "honour max_memos_per_run like every run" while still always producing a body. A real run never
    reaches this with an over-cap selection: its plausibility guard has already failed the run.
    """
    included = selection.included[: settings.max_memos_per_run]
    rendered = RenderedDigest(selected=included, deferred=list(selection.deferred))
    if not included:
        return rendered

    attachment_ids = selection_mod.unresolved_attachment_ids(
        selection_mod.Selection(included=included, deferred=[])
    )
    existing = await existing_hosted_links(db, attachment_ids)
    rendered.links = await hosting.resolve_links(
        included,
        host=host if upload else None,
        existing=existing,
        max_videos_per_memo=settings.max_videos_per_memo,
        link_images_full=settings.link_images_full,
    )
    if upload and rendered.links.uploaded:
        await record_hosted_media(db, rendered.links.uploaded)

    files = await render.read_card_files(
        render.card_source_paths(included, rendered.links, settings.max_videos_per_memo)
    )
    cards, inline = render.build_cards(
        included,
        files,
        rendered.links,
        max_videos_per_memo=settings.max_videos_per_memo,
        link_images_full=settings.link_images_full,
        blocked_hosts=render.memos_ng_hosts(),
    )
    rendered.inline_by_cid = {image.cid: image for image in inline}
    rendered.day_groups = render.group_by_local_day(cards, settings.timezone)
    rendered.date_range = render.subject_date_range(rendered.day_groups)
    split = split_into_parts(
        rendered.day_groups, build_cid_to_bytes(inline), settings.max_email_bytes
    )
    rendered.parts = split.parts
    rendered.deferred.extend(Deferral(uid=uid, reason=reason) for uid, reason in split.deferred)
    return rendered


def part_payload(
    rendered: RenderedDigest, part_index: int, settings: DigestSettings
) -> tuple[str, str, str, list[InlineImage], list[int], set[int]]:
    """`(subject, html, plain, inline images, memo ids, incomplete memo ids)` for one part."""
    part = rendered.parts[part_index - 1]
    total = len(rendered.parts)
    cards = part.cards()
    subject = render.subject_for(
        settings.mail_title, rendered.date_range, part_index, total
    )
    html = render.render_html(
        part.day_groups,
        subject=subject,
        mail_title=settings.mail_title,
        date_range=rendered.date_range,
        memo_count=len(cards),
        part_index=part_index,
        part_total=total,
    )
    plain = render.render_plain(
        part.day_groups,
        subject=subject,
        memo_count=len(cards),
        part_index=part_index,
        part_total=total,
    )
    images = [
        rendered.inline_by_cid[cid]
        for cid in part.inline_image_cids
        if cid in rendered.inline_by_cid
    ]
    incomplete = {card.memo_id for card in cards if card.is_incomplete or card.media_incomplete}
    return subject, html, plain, images, [card.memo_id for card in cards], incomplete


# --- the Dropbox host a real run builds -----------------------------------------------------------


def dropbox_configured() -> bool:
    return all(
        (
            app_settings.DROPBOX_APP_KEY,
            app_settings.DROPBOX_APP_SECRET,
            app_settings.DROPBOX_REFRESH_TOKEN,
        )
    )


def run_folder_name(now_utc: datetime, timezone: str) -> str:
    """`YYYY-MM-DD_HHMM` in the configured zone — the satellite's folder naming, kept so the two
    tools' uploads read as one history in the Dropbox folder they share."""
    zone = resolve_zone(timezone) or ZoneInfo(FALLBACK_TIMEZONE)
    return now_utc.astimezone(zone).strftime("%Y-%m-%d_%H%M")


def make_dropbox_host_factory(
    settings: DigestSettings, now_utc: datetime
) -> HostFactory:
    """A factory that builds and authenticates a `DropboxHost`, or returns None.

    None is the inline-only path, and three different causes reach it identically: hosting off in
    the settings, the env keys unset, and a token refresh that failed. Only the last is a surprise,
    so only it logs a warning — and the run continues either way rather than failing, which is the
    behaviour AC 8 asks for.
    """

    async def factory(on_operation: hosting.Operation) -> MediaHost | None:
        if not settings.hosting_enabled or not dropbox_configured():
            return None
        client = httpx.AsyncClient()
        host = DropboxHost(
            app_key=app_settings.DROPBOX_APP_KEY,
            app_secret=app_settings.DROPBOX_APP_SECRET,
            refresh_token=app_settings.DROPBOX_REFRESH_TOKEN,
            base_folder=settings.dropbox_folder,
            run_folder=run_folder_name(now_utc, settings.timezone),
            client=client,
            on_operation=on_operation,
        )
        try:
            await host.refresh()
        except (hosting.DropboxError, httpx.HTTPError):
            await client.aclose()
            logger.warning("digest.hosting_unavailable", reason="token refresh failed")
            return None
        return host

    return factory


def default_transport() -> MailTransport:
    return SmtplibTransport(
        SmtpSettings(
            host=app_settings.SMTP_HOST,
            port=app_settings.SMTP_PORT,
            username=app_settings.SMTP_USERNAME,
            password=app_settings.SMTP_PASSWORD,
        )
    )


# --- the run --------------------------------------------------------------------------------------


async def run_digest(
    *,
    trigger: str = DigestTrigger.scheduled,
    session_factory: SessionFactory = async_session,
    transport: MailTransport | None = None,
    host_factory: HostFactory | None = None,
    now_utc: datetime | None = None,
) -> RunResult | None:
    """Perform one digest run. Returns None when the run was aborted by a lost claim.

    Raises `RunSkipped` when another run already owns the slot — the caller decides whether that
    is "skip" (the worker) or 409 (the manual endpoint).
    """
    moment = now_utc or datetime.now(UTC)
    async with session_factory() as db:
        settings = await load_settings(db)
        run_id = await open_run(
            db, trigger=trigger, local_date=local_date_of(moment, settings.timezone)
        )

    # A SECOND session for the run itself. The first one's only job was to commit the lock, and a
    # run holds its session for minutes across SMTP and Dropbox calls — keeping those apart means
    # the lock is committed and durable before anything long-running starts.
    async with session_factory() as db:
        return await _execute_run(
            db,
            run_id=run_id,
            moment=moment,
            transport=transport,
            host_factory=host_factory,
        )


async def _execute_run(
    db: AsyncSession,
    *,
    run_id: int,
    moment: datetime,
    transport: MailTransport | None,
    host_factory: HostFactory | None,
) -> RunResult | None:
    settings = await load_settings(db)
    recipients = await enabled_recipients(db)
    sender = sender_address(settings)

    # Capability and plausibility guards, all BEFORE any render or upload work (LSN-0022: never
    # claim work you structurally cannot complete). Each writes a `failed` ROW rather than staying
    # silent, so the failure is visible in the Runs table, in the metrics and to the 26 h alert —
    # the silent-failure class this whole feature exists to end.
    for condition, message in (
        (not smtp_configured(), ERROR_SMTP_UNCONFIGURED),
        (not recipients, ERROR_NO_RECIPIENTS),
        (sender.lower() in {r.lower() for r in recipients}, ERROR_SENDER_IS_RECIPIENT),
    ):
        if condition:
            await finish_run(db, run_id, status=FAILED, error=message)
            logger.warning("digest.run_refused", run_id=run_id, reason=message)
            return RunResult(
                run_id=run_id, status=FAILED, parts=0, parts_sent=0, memos_sent=0, error=message
            )

    candidates = await selection_mod.select_for_run(db, settings, moment)
    if len(candidates.included) > settings.max_memos_per_run:
        message = (
            f"selection {len(candidates.included)} exceeds max_memos_per_run "
            f"{settings.max_memos_per_run}"
        )
        await finish_run(db, run_id, status=FAILED, error=message)
        logger.warning("digest.run_refused", run_id=run_id, reason=message)
        return RunResult(
            run_id=run_id, status=FAILED, parts=0, parts_sent=0, memos_sent=0, error=message
        )

    async def beat() -> None:
        await heartbeat(db, run_id)

    host = None
    if host_factory is not None:
        host = await host_factory(beat)
    rendered = await build_digest(db, settings, candidates, host=host, upload=True)

    if not rendered.parts:
        status = DEFERRED if rendered.deferred else NOTHING_TO_SEND
        await finish_run(db, run_id, status=status)
        return RunResult(run_id=run_id, status=status, parts=0, parts_sent=0, memos_sent=0)

    await record_progress(db, run_id, parts=len(rendered.parts), recipients=len(recipients))
    active_transport = transport or default_transport()

    errors: list[str] = []
    parts_sent = 0
    memos_sent = 0
    for part_index in range(1, len(rendered.parts) + 1):
        if not await claim_part(db, run_id, part_index):
            logger.warning("digest.claim_lost", run_id=run_id, part=part_index)
            return None
        subject, html, plain, images, memo_ids, incomplete = part_payload(
            rendered, part_index, settings
        )
        message = build_message(
            subject=subject,
            from_email=sender,
            from_name=settings.from_name,
            html_body=html,
            plain_body=plain,
            inline_images=images,
        )
        try:
            refusals = await active_transport.send(message, sender, recipients)
        except Exception as exc:  # any transport failure ends the RUN, never the worker
            errors.append(f"part {part_index}: send failed ({type(exc).__name__})")
            logger.warning(
                "digest.part_send_failed", run_id=run_id, part=part_index, error=str(exc)
            )
            break
        await beat()
        if accepted_count(recipients, refusals) == 0:
            errors.append(f"part {part_index}: refused by all {len(recipients)} recipient(s)")
            logger.warning("digest.part_refused_by_all", run_id=run_id, part=part_index)
            break
        try:
            await commit_part(
                db,
                run_id,
                part_index=part_index,
                memo_ids=memo_ids,
                incomplete_ids=incomplete,
            )
        except RunLost:
            logger.warning("digest.commit_lost", run_id=run_id, part=part_index)
            return None
        parts_sent += 1
        memos_sent += len(memo_ids)
        if refusals:
            errors.append(refusal_note(part_index, refusals))

    status = SENT if parts_sent == len(rendered.parts) else FAILED
    error = "\n".join(errors) if errors else None
    await finish_run(db, run_id, status=status, memos_sent=memos_sent, error=error)
    logger.info(
        "digest.run_finished",
        run_id=run_id,
        status=status,
        parts=len(rendered.parts),
        parts_sent=parts_sent,
        memos=memos_sent,
    )
    return RunResult(
        run_id=run_id,
        status=status,
        parts=len(rendered.parts),
        parts_sent=parts_sent,
        memos_sent=memos_sent,
        error=error,
    )


async def run_scheduled_digest_safe(
    *,
    session_factory: SessionFactory = async_session,
    transport: MailTransport | None = None,
    now_utc: datetime | None = None,
) -> RunResult | None:
    """The worker's entry point: a scheduled run with EVERY error isolated.

    The same boundary guard `run_frame_share_drain` and `run_media_recover_safe` already apply —
    a digest failure must never be able to halt media processing. `RunSkipped` is the ordinary
    outcome of a race and is not an error.
    """
    try:
        moment = now_utc or datetime.now(UTC)
        async with session_factory() as db:
            settings = await load_settings(db)
            factory = make_dropbox_host_factory(settings, moment)
        return await run_digest(
            trigger=DigestTrigger.scheduled,
            session_factory=session_factory,
            transport=transport,
            host_factory=factory,
            now_utc=moment,
        )
    except RunSkipped:
        logger.info("digest.run_skipped_another_in_flight")
        return None
    except Exception:
        logger.exception("digest.run_error")
        return None


# --- the retention sweep ------------------------------------------------------------------------


async def sweep_hosted_media_safe(
    *,
    session_factory: SessionFactory = async_session,
    host_factory: HostFactory | None = None,
) -> int:
    """Delete hosted files past `retention_days` and stamp `swept_at`. Errors isolated.

    Age is measured from `uploaded_at`, which is safe because deliveries are one-shot: a memo is
    selected at most once, so no LATER day's mail can reference an older upload. The one exception
    is the duplicate window in the delivery guarantee, which re-references uploads a few days old
    at most — harmless at a 30-day retention.
    """
    try:
        async with session_factory() as db:
            settings = await load_settings(db)
            retention_days = settings.retention_days
            rows = await db.execute(
                sa_text(
                    "SELECT attachment_id, dropbox_path FROM digest_hosted_media "
                    "WHERE swept_at IS NULL "
                    "AND uploaded_at < now() - make_interval(days => :days)"
                ),
                {"days": retention_days},
            )
            due = [(row[0], row[1]) for row in rows]
            if not due:
                return 0
            factory = host_factory or make_dropbox_host_factory(settings, datetime.now(UTC))

            async def beat() -> None:
                return None

            host = await factory(beat)
            if host is None:
                logger.info("digest.retention_sweep_skipped", reason="hosting unavailable")
                return 0
            swept = 0
            for attachment_id, dropbox_path in due:
                try:
                    await host.delete(dropbox_path)
                except (hosting.DropboxError, httpx.HTTPError):
                    logger.warning("digest.retention_delete_failed", attachment_id=attachment_id)
                    continue
                # Stamped only after the file is actually gone from Dropbox — `swept_at` is the
                # claim that it IS gone, and stamping it for a failed delete would leave a live
                # file no sweep ever looks at again.
                await db.execute(
                    sa_text(
                        "UPDATE digest_hosted_media SET swept_at = now() "
                        "WHERE attachment_id = :id"
                    ),
                    {"id": attachment_id},
                )
                swept += 1
            await db.commit()
            if swept:
                logger.info(
                    "digest.retention_swept", count=swept, retention_days=retention_days
                )
            return swept
    except Exception:
        logger.exception("digest.retention_sweep_error")
        return 0


# --- the probes the admin panel drives ------------------------------------------------------------


async def preview(
    db: AsyncSession, *, now_utc: datetime | None = None
) -> tuple[RenderedDigest, DigestSettings]:
    """Render what would go out now. Persists nothing and uploads nothing."""
    settings = await load_settings(db)
    moment = now_utc or datetime.now(UTC)
    selection = await selection_mod.select_for_run(db, settings, moment)
    return await build_digest(db, settings, selection, host=None, upload=False), settings


async def test_send(
    db: AsyncSession,
    *,
    address: str,
    empty: bool = False,
    transport: MailTransport | None = None,
    now_utc: datetime | None = None,
) -> int:
    """Send ONE real mail to `address` and nothing else. Returns the memo count it carried.

    Never to `digest_recipients`, in any configuration — the address is the calling admin's own,
    resolved by the handler from the authenticated session and not from any request field.

    It ALWAYS sends, even with an empty selection (the body then says so), because its job is to
    be a usable SMTP probe. `empty=True` renders that body directly without consulting the
    selection at all, which is the probe Phase 4 needs with no settings write anywhere in it.
    """
    settings = await load_settings(db)
    moment = now_utc or datetime.now(UTC)
    active_transport = transport or default_transport()
    sender = sender_address(settings)
    if empty:
        html, plain = render.render_empty(settings.mail_title)
        message = build_message(
            subject=settings.mail_title,
            from_email=sender,
            from_name=settings.from_name,
            html_body=html,
            plain_body=plain,
            inline_images=[],
        )
        await active_transport.send(message, sender, [address])
        return 0

    selection = await selection_mod.select_for_run(db, settings, moment)
    rendered = await build_digest(db, settings, selection, host=None, upload=False)
    if not rendered.parts:
        html, plain = render.render_empty(settings.mail_title)
        message = build_message(
            subject=settings.mail_title,
            from_email=sender,
            from_name=settings.from_name,
            html_body=html,
            plain_body=plain,
            inline_images=[],
        )
        await active_transport.send(message, sender, [address])
        return 0

    memo_count = 0
    for part_index in range(1, len(rendered.parts) + 1):
        subject, html, plain, images, memo_ids, _ = part_payload(
            rendered, part_index, settings
        )
        message = build_message(
            subject=subject,
            from_email=sender,
            from_name=settings.from_name,
            html_body=html,
            plain_body=plain,
            inline_images=images,
        )
        await active_transport.send(message, sender, [address])
        memo_count += len(memo_ids)
    return memo_count
