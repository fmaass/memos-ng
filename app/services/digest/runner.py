"""One digest run: the row that IS the lock, the claim CAS, the per-part commit, and the sweeps.

Five rules run through everything here and each was bought by a named failure mode.

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

**No transaction is ever open across an external boundary.** Every database read here is taken in
a short session that is CLOSED before the first filesystem, Dropbox or SMTP call, and every write
reopens one of its own. A SQLAlchemy `SELECT` autobegins a transaction, so a session merely held
open pins a pooled connection and an idle transaction for the whole of a run that legitimately
lasts hours — against a bounded pool shared with production (ADR-0009), and against this worker's
own commit-before-long-work rule. The settings are therefore read once into a frozen
`DigestConfig` snapshot rather than carried as a mapped instance, which could not be read after
its session closed anyway.

**Every hosted file is owned by a committed row BEFORE it exists.** The upload path is claim →
upload → mark, one file at a time (`hosting.resolve_links`), so a crash can leave a `pending` row
whose remote object may or may not be there — a question the retention sweep resolves by asking
Dropbox — but can never leave an object that no row names. That is the difference between a
cleanable orphan and an unfindable one.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.database import async_session
from app.models import DigestRunStatus, DigestTrigger, HostedMediaStatus
from app.services.digest import domain, hosting, render
from app.services.digest import select as selection_mod
from app.services.digest.domain import DigestConfig, sender_address
from app.services.digest.gate import FALLBACK_TIMEZONE, LEDGER_TRIGGERS, local_date_of, resolve_zone
from app.services.digest.hosting import DropboxHost, HostedLedger, MediaHost, MediaLinks
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

# The run vocabulary, aliased from the ONE enum that defines it (`app.models`). Aliases rather
# than string literals: a status spelled twice is a status that can be spelled differently twice.
SENT = DigestRunStatus.sent
NOTHING_TO_SEND = DigestRunStatus.nothing_to_send
DEFERRED = DigestRunStatus.deferred
FAILED = DigestRunStatus.failed

# Fifteen minutes of SILENCE, not of elapsed time. A healthy run stamps `heartbeat_at` before and
# after every part send and after every Dropbox upload-session operation, so the longest gap it
# can leave is one operation (≤ 10 min at the 600 s per-operation timeout) — comfortably below
# this bar, and unrelated to how long the whole run takes.
STALE_RUN_MINUTES = 15

# How long a hosted-media row may sit `pending` before the sweep asks Dropbox what actually
# happened. Comfortably above the longest single upload (one 600 s upload-session operation per
# 48 MB chunk), so a live upload is never reconciled out from under itself.
PENDING_RECONCILE_MINUTES = 60

ERROR_SMTP_UNCONFIGURED = "smtp not configured"
ERROR_NO_RECIPIENTS = "no enabled recipients"
ERROR_SENDER_IS_RECIPIENT = "the sender address is also a recipient"
ERROR_RUNNER_DIED = "runner died"

# A plain callable, not `async_sessionmaker`: every caller here uses it the same way — call it,
# get a session, close the session — and typing it structurally is what lets a test hand in a
# factory that COUNTS the transactions its sessions hold open (which is how "no transaction is
# open during an upload" becomes an assertion rather than a claim).
SessionFactory = Callable[[], AsyncSession]
HostFactory = Callable[[hosting.Operation], Awaitable[MediaHost | None]]


class RunSkipped(RuntimeError):
    """Another run already owns this slot — the INSERT lost to a unique partial index."""


class RunLost(RuntimeError):
    """A fenced CAS updated 0 rows: the sweep closed this run, or another actor advanced it."""


class SenderIsRecipient(RuntimeError):
    """The destination address is the address the mail is sent FROM.

    Its own type because two callers must answer it differently — a run writes a `failed` row, a
    test-send answers 422 — while the RULE is one rule and lives in one place.
    """


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


def _sql_values(values: tuple[str, ...]) -> str:
    """`('a', 'b')` for an IN list built from CONSTANTS of this module.

    Never from caller input: these are enum members, so the rendered literal is a constant of the
    ledger and not a parameter. It is rendered rather than bound because the same list has to sit
    inside a `NOT EXISTS` sub-select the INSERT re-evaluates, where a bound array would change the
    statement's shape for no gain.
    """
    return "(" + ", ".join(f"'{value}'" for value in sorted(values)) + ")"


_LEDGER_TRIGGER_SQL = _sql_values(tuple(LEDGER_TRIGGERS))


# --- the run row, and every fenced write against it ---------------------------------------------


# A scheduled run's INSERT re-checks the day ledger inside the statement. `local_date` is bound
# twice (the row it writes and the ledger it tests), which is why the trigger list is rendered
# from `gate.LEDGER_TRIGGERS`: it is a constant of the ledger, not a caller's input.
_INSERT_SCHEDULED = (
    "INSERT INTO digest_runs (trigger, local_date) "
    "SELECT :trigger, :local_date "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM digest_runs "
    f"  WHERE local_date = :local_date AND trigger IN {_LEDGER_TRIGGER_SQL}"
    ") RETURNING id"
)
_INSERT_PLAIN = (
    "INSERT INTO digest_runs (trigger, local_date) VALUES (:trigger, :local_date) RETURNING id"
)


async def open_run(db: AsyncSession, *, trigger: str, local_date: date) -> int:
    """INSERT and COMMIT the run row before any other work. Raises `RunSkipped` on a conflict.

    The conflict IS the answer, never a pre-flight "is anything running?" read: a check-then-insert
    is exactly the TOCTOU race the unique partial indexes exist to close, and two concurrent callers
    would both pass it.

    **A SCHEDULED insert additionally re-tests the day ledger in the same statement.**
    `uq_digest_runs_scheduled_local_date` covers `trigger = 'scheduled'` ONLY, so it cannot see a
    manual run — and a manual run that finishes between the worker's gate read and this insert
    would otherwise let a second digest go out the same day, to the same six mailboxes, with the
    memos the manual run did not reach. The gate's ledger read is an optimisation; this predicate
    is the guarantee, and it lives in the INSERT rather than beside it for exactly the reason the
    unique indexes do. Zero rows inserted means "today already had its attempt" — skip, do not
    send.
    """
    scheduled = trigger == DigestTrigger.scheduled
    try:
        run_id = await db.scalar(
            sa_text(_INSERT_SCHEDULED if scheduled else _INSERT_PLAIN),
            {"trigger": trigger, "local_date": local_date},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise RunSkipped(str(exc.orig)) from exc
    if run_id is None:
        # Only the scheduled statement can insert nothing; a plain INSERT either returns an id
        # or raises. `assert` would be wrong here — this is an ordinary outcome, not a broken
        # invariant.
        raise RunSkipped(f"{local_date} already has a scheduled or manual run")
    return int(run_id)


async def claim_run(db: AsyncSession, run_id: int) -> bool:
    """Adopt an already-committed, not-yet-started run — the manual `run` endpoint's row.

    The endpoint COMMITS the row and returns; the worker executes it. Between those two moments
    the row is in flight but unowned, and `heartbeat_at IS NULL` is exactly what "nobody has
    started it" means (a running run stamps liveness before it does anything else). Making the
    adoption a CAS on that column is what keeps a second worker from executing the same row
    beside the first: the INSERT's unique index no longer separates them, because the row they
    would race over already exists.

    A row nobody adopts is not lost: the stale-run sweep judges it on `COALESCE(heartbeat_at,
    started_at)` and marks it `failed` after the same 15 minutes of silence, which is the right
    answer when there is no worker to run it.
    """
    adopted = await db.scalar(
        sa_text(
            "UPDATE digest_runs SET heartbeat_at = now() "
            "WHERE id = :id AND status IS NULL AND heartbeat_at IS NULL RETURNING id"
        ),
        {"id": run_id},
    )
    await db.commit()
    return adopted is not None


async def pending_manual_run_id(db: AsyncSession) -> int | None:
    """The oldest manual run row that is in flight and has not been started by anyone."""
    return await db.scalar(
        sa_text(
            "SELECT id FROM digest_runs WHERE status IS NULL AND trigger = :trigger "
            "AND heartbeat_at IS NULL ORDER BY id ASC LIMIT 1"
        ),
        {"trigger": DigestTrigger.manual},
    )


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


async def sweep_stale_runs_safe(
    session_factory: SessionFactory = async_session, *, stale_minutes: int = STALE_RUN_MINUTES
) -> int:
    """`sweep_stale_runs` with ALL errors isolated — the boundary guard every periodic sweep in the
    worker loop gets (`run_media_recover_safe`, `run_frame_share_recover_safe`,
    `sweep_job_progress_safe`).

    Without it a transient database error inside the sweep propagates out of `worker.run()` and
    stops the whole worker: no heartbeat, no media drain, no frame drain — the optional digest
    feature taking down the media pipeline, which is the one thing every other sweep here is
    written to prevent. Returns rows swept, 0 on error.
    """
    try:
        return await sweep_stale_runs(session_factory, stale_minutes=stale_minutes)
    except Exception:
        logger.exception("digest.stale_sweep_error")
        return 0


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
    predating the Phase-2 migration, every run still in its first upload, and a manual row no
    worker has adopted — on the same bar.
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


async def load_settings(db: AsyncSession) -> DigestConfig:
    """The settings singleton as a frozen snapshot — see `domain.DigestConfig`."""
    return await domain.load_config(db)


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
            f"SELECT max(local_date) FROM digest_runs WHERE trigger IN {_LEDGER_TRIGGER_SQL}"
        )
    )


async def existing_hosted_links(db: AsyncSession, attachment_ids: list[int]) -> dict[int, str]:
    """Links already recorded for these attachments and not yet swept.

    A test-send and a preview render from these and upload nothing: a probe must never create a
    Dropbox file the ledger does not know about.

    `uploaded` only, and the NEWEST per attachment: a `pending` row promises no link at all, a
    `swept` row's object is gone, and one attachment can carry several rows because a later run
    hosts it again under a new run folder.
    """
    if not attachment_ids:
        return {}
    rows = await db.execute(
        sa_text(
            "SELECT DISTINCT ON (attachment_id) attachment_id, shared_link "
            "FROM digest_hosted_media "
            "WHERE attachment_id = ANY(:ids) AND status = :status AND shared_link IS NOT NULL "
            "ORDER BY attachment_id, uploaded_at DESC, id DESC"
        ),
        {"ids": attachment_ids, "status": HostedMediaStatus.uploaded},
    )
    return {row[0]: row[1] for row in rows}


class SessionLedger:
    """The hosted-media ledger, one short committing transaction per write.

    Not a session held by the hosting pass: the whole reason this exists is that the upload
    between the two writes must happen with NO transaction open, so each write opens its own,
    commits and closes.
    """

    def __init__(self, session_factory: SessionFactory = async_session) -> None:
        self._session_factory = session_factory

    async def claim(self, *, attachment_id: int, kind: str, dropbox_path: str) -> int:
        """Commit `dropbox_path` as `pending` and return its ledger row id.

        Idempotent on the path, which is the natural key of the remote object. A conflicting row
        that is still `pending` is RE-OPENED (a previous attempt died mid-upload; this attempt now
        owns the intention and its clock restarts), while one that is already `uploaded` is left
        exactly as it is — a row that promises a live object must not be talked back into
        promising nothing.
        """
        async with self._session_factory() as db:
            row_id = await db.scalar(
                sa_text(
                    "INSERT INTO digest_hosted_media "
                    "(attachment_id, kind, dropbox_path, status, created_at) "
                    "VALUES (:attachment_id, :kind, :dropbox_path, :pending, now()) "
                    "ON CONFLICT (dropbox_path) DO UPDATE SET "
                    "attachment_id = EXCLUDED.attachment_id, kind = EXCLUDED.kind, "
                    "status = :pending, created_at = now(), uploaded_at = NULL, "
                    "shared_link = NULL, swept_at = NULL "
                    "WHERE digest_hosted_media.status <> :uploaded "
                    "RETURNING id"
                ),
                {
                    "attachment_id": attachment_id,
                    "kind": kind,
                    "dropbox_path": dropbox_path,
                    "pending": HostedMediaStatus.pending,
                    "uploaded": HostedMediaStatus.uploaded,
                },
            )
            if row_id is None:
                # The conflicting row is already `uploaded`, so the DO UPDATE's WHERE excluded it
                # and RETURNING yielded nothing. The row still IS the claim on this path; read its
                # id so the caller has something to mark, and let `mark_uploaded`'s CAS decline.
                row_id = await db.scalar(
                    sa_text("SELECT id FROM digest_hosted_media WHERE dropbox_path = :path"),
                    {"path": dropbox_path},
                )
            await db.commit()
        if row_id is None:  # pragma: no cover — the row was committed a statement ago
            raise RuntimeError(f"hosted-media claim for {dropbox_path} vanished")
        return int(row_id)

    async def mark_uploaded(self, ledger_id: int, *, shared_link: str, size: int) -> None:
        """`pending` → `uploaded`, immediately after THIS file's upload succeeded.

        A CAS on the previous status, so the transition is write-once: a row that is already
        `uploaded` or has been `swept` is not rewritten, and the mismatch is logged rather than
        raised — the bytes are on Dropbox either way and failing the run here would help nobody.
        """
        async with self._session_factory() as db:
            updated = await db.scalar(
                sa_text(
                    "UPDATE digest_hosted_media SET status = :uploaded, shared_link = :link, "
                    "bytes = :bytes, uploaded_at = now() "
                    "WHERE id = :id AND status = :pending RETURNING id"
                ),
                {
                    "id": ledger_id,
                    "link": shared_link,
                    "bytes": size,
                    "pending": HostedMediaStatus.pending,
                    "uploaded": HostedMediaStatus.uploaded,
                },
            )
            await db.commit()
        if updated is None:
            logger.warning("digest.hosted_media_transition_lost", ledger_id=ledger_id)


# --- building the mail ---------------------------------------------------------------------------


def smtp_configured() -> bool:
    """Re-exported from `domain` so callers of this module keep one import."""
    return domain.smtp_configured()


def dropbox_configured() -> bool:
    return domain.dropbox_configured()


async def build_digest(
    session_factory: SessionFactory,
    settings: DigestConfig,
    selection: selection_mod.Selection,
    *,
    host: MediaHost | None,
    ledger: HostedLedger | None = None,
) -> RenderedDigest:
    """Host, render and split an already-made selection — everything up to (not including) SMTP.

    `host=None` is the probe contract: links are resolved from the hosted-media ledger alone and
    no Dropbox call is made, so a preview or a test-send can never leave a hosted file behind.

    The one database read it needs — which attachments already have a live link — happens in a
    session that is closed again before any upload or file read starts.

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
    async with session_factory() as db:
        existing = await existing_hosted_links(db, attachment_ids)
    rendered.links = await hosting.resolve_links(
        included,
        host=host,
        ledger=ledger,
        existing=existing,
        max_videos_per_memo=settings.max_videos_per_memo,
        link_images_full=settings.link_images_full,
    )

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
    rendered: RenderedDigest, part_index: int, settings: DigestConfig
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


def run_folder_name(now_utc: datetime, timezone: str) -> str:
    """`YYYY-MM-DD_HHMM` in the configured zone — the satellite's folder naming, kept so the two
    tools' uploads read as one history in the Dropbox folder they share."""
    zone = resolve_zone(timezone) or ZoneInfo(FALLBACK_TIMEZONE)
    return now_utc.astimezone(zone).strftime("%Y-%m-%d_%H%M")


def make_dropbox_host_factory(settings: DigestConfig, now_utc: datetime) -> HostFactory:
    """A factory that builds and authenticates a `DropboxHost`, or returns None.

    None is the inline-only path, and three causes reach it: hosting off in the settings, the env
    keys unset, and a token refresh that failed. The run continues in all three rather than failing
    and says so in one line of its own — see `_execute_run`. Only the surprising causes log here,
    because "hosting is switched off" is a decision, not an incident.
    """

    async def factory(on_operation: hosting.Operation) -> MediaHost | None:
        if not settings.hosting_enabled:
            return None
        if not domain.dropbox_configured():
            logger.warning("digest.hosting_keys_missing")
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
        except Exception:
            # ANY failure, not only a Dropbox or transport error: a malformed token response
            # raises `KeyError`/`JSONDecodeError` out of `refresh()`, and a client left unclosed
            # on that path leaks a connection pool once per run in a process that lives for weeks.
            await client.aclose()
            # The CAUSE, under its own event name: the per-run "inline-only" line is emitted by
            # the run itself, so that one stays exactly one per run whatever brought hosting down.
            logger.warning("digest.hosting_token_refresh_failed")
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
    """Open a run row and perform it. Returns None when the run was aborted by a lost claim.

    Raises `RunSkipped` when another run already owns the slot — the caller decides whether that
    is "skip" (the worker) or a refusal (the manual endpoint).
    """
    moment = now_utc or datetime.now(UTC)
    async with session_factory() as db:
        settings = await load_settings(db)
        run_id = await open_run(
            db, trigger=trigger, local_date=local_date_of(moment, settings.timezone)
        )
    return await _execute_run(
        run_id=run_id,
        moment=moment,
        session_factory=session_factory,
        transport=transport,
        host_factory=host_factory,
    )


async def _execute_run(
    *,
    run_id: int,
    moment: datetime,
    session_factory: SessionFactory,
    transport: MailTransport | None,
    host_factory: HostFactory | None,
) -> RunResult | None:
    async with session_factory() as db:
        settings = await load_settings(db)
        recipients = await enabled_recipients(db)
    sender = sender_address(settings)

    # Capability and plausibility guards, all BEFORE any render or upload work (LSN-0022: never
    # claim work you structurally cannot complete). Each writes a `failed` ROW rather than staying
    # silent, so the failure is visible in the Runs table, in the metrics and to the 26 h alert —
    # the silent-failure class this whole feature exists to end.
    for condition, message in (
        (not domain.smtp_configured(), ERROR_SMTP_UNCONFIGURED),
        (not recipients, ERROR_NO_RECIPIENTS),
        (sender.lower() in {r.lower() for r in recipients}, ERROR_SENDER_IS_RECIPIENT),
    ):
        if condition:
            async with session_factory() as db:
                await finish_run(db, run_id, status=FAILED, error=message)
            logger.warning("digest.run_refused", run_id=run_id, reason=message)
            return RunResult(
                run_id=run_id, status=FAILED, parts=0, parts_sent=0, memos_sent=0, error=message
            )

    candidates = await selection_mod.select_for_run(session_factory, settings, moment)
    if len(candidates.included) > settings.max_memos_per_run:
        message = (
            f"selection {len(candidates.included)} exceeds max_memos_per_run "
            f"{settings.max_memos_per_run}"
        )
        async with session_factory() as db:
            await finish_run(db, run_id, status=FAILED, error=message)
        logger.warning("digest.run_refused", run_id=run_id, reason=message)
        return RunResult(
            run_id=run_id, status=FAILED, parts=0, parts_sent=0, memos_sent=0, error=message
        )

    async def beat() -> None:
        async with session_factory() as db:
            await heartbeat(db, run_id)

    host = None
    if host_factory is not None:
        host = await host_factory(beat)
    if host is None and settings.hosting_enabled:
        # ONE line per run, and it is the CONSEQUENCE rather than the cause: hosting was asked for
        # and is not available, so this mail goes out inline-only with the "video not hosted" chip
        # on every video card. The cause (keys unset, token refresh failed) is logged where it is
        # known; this is what an operator reading the run back needs to see. "Logged, never fatal"
        # is the contract — the run continues.
        logger.warning(
            "digest.hosting_unavailable",
            run_id=run_id,
            detail="hosting is enabled but no media host is available; sending inline-only",
        )
    try:
        rendered = await build_digest(
            session_factory,
            settings,
            candidates,
            host=host,
            ledger=SessionLedger(session_factory),
        )
    finally:
        # Every path, not just the happy one: the host owns an httpx client, and a run that raised
        # between here and the end would leak it once per day for as long as the fault lasted.
        if host is not None:
            await host.aclose()

    if not rendered.parts:
        status = DEFERRED if rendered.deferred else NOTHING_TO_SEND
        async with session_factory() as db:
            await finish_run(db, run_id, status=status)
        return RunResult(run_id=run_id, status=status, parts=0, parts_sent=0, memos_sent=0)

    async with session_factory() as db:
        await record_progress(db, run_id, parts=len(rendered.parts), recipients=len(recipients))
    active_transport = transport or default_transport()

    errors: list[str] = []
    parts_sent = 0
    memos_sent = 0
    for part_index in range(1, len(rendered.parts) + 1):
        async with session_factory() as db:
            claimed = await claim_part(db, run_id, part_index)
        if not claimed:
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
            async with session_factory() as db:
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
    async with session_factory() as db:
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
    """The worker's entry point for a SCHEDULED run, with EVERY error isolated.

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


async def run_claimed_digest_safe(
    run_id: int,
    *,
    session_factory: SessionFactory = async_session,
    transport: MailTransport | None = None,
    now_utc: datetime | None = None,
) -> RunResult | None:
    """Execute a run row somebody else committed — the manual endpoint's, adopted by this worker.

    The API COMMITS the claim and answers 202; the pipeline runs HERE, in the worker, because the
    work takes minutes to hours (Dropbox upload-session operations alone allow 600 s each) and a
    request lifetime is not the right lock for it. Errors are isolated for the same reason
    `run_scheduled_digest_safe` isolates them: the digest is optional, the media pipeline is not.
    """
    try:
        moment = now_utc or datetime.now(UTC)
        async with session_factory() as db:
            settings = await load_settings(db)
        factory = make_dropbox_host_factory(settings, moment)
        return await _execute_run(
            run_id=run_id,
            moment=moment,
            session_factory=session_factory,
            transport=transport,
            host_factory=factory,
        )
    except Exception:
        logger.exception("digest.run_error", run_id=run_id)
        return None


# --- the retention sweep ------------------------------------------------------------------------


@dataclass(frozen=True)
class _DueMedia:
    """One ledger row the sweep has to resolve, read out as a plain value so the session that read
    it can be closed before Dropbox is contacted."""

    id: int
    dropbox_path: str
    status: str


async def sweep_hosted_media_safe(
    *,
    session_factory: SessionFactory = async_session,
    host_factory: HostFactory | None = None,
) -> int:
    """Resolve every hosted-media row that is due, and stamp each one in its own transaction.

    Two kinds of row are due, and the sweep is the only thing that closes either:

    * `uploaded` past `retention_days` — delete the remote object, then stamp `swept`. Age is
      measured from `uploaded_at`, which is safe because deliveries are one-shot: a memo is
      selected at most once, so no LATER day's mail can reference an older upload. The one
      exception is the duplicate window in the delivery guarantee, which re-references uploads a
      few days old at most — harmless at a 30-day retention.
    * `pending` past `PENDING_RECONCILE_MINUTES` — a process died between committing the intention
      and recording the result, so the remote is the only authority on what happened: present
      means the upload landed (record it, and let ordinary retention sweep it later), absent means
      it never did (stamp it `swept`, because there is nothing to delete).

    The rows are read in a session that is CLOSED before the first Dropbox call, and each stamp
    reopens its own — nothing here holds a transaction across the network. Errors are isolated:
    returns the number of rows whose remote object is now gone, 0 on error.
    """
    try:
        async with session_factory() as db:
            settings = await load_settings(db)
            due = await _due_hosted_media(db, retention_days=settings.retention_days)
        if not due:
            return 0
        factory = host_factory or make_dropbox_host_factory(settings, datetime.now(UTC))

        async def beat() -> None:
            return None

        host = await factory(beat)
        if host is None:
            logger.info("digest.retention_sweep_skipped", reason="hosting unavailable")
            return 0
        try:
            swept = await _resolve_due(session_factory, host, due)
        finally:
            await host.aclose()  # every path: the sweep owns this host's client
        if swept:
            logger.info(
                "digest.retention_swept", count=swept, retention_days=settings.retention_days
            )
        return swept
    except Exception:
        logger.exception("digest.retention_sweep_error")
        return 0


async def _due_hosted_media(db: AsyncSession, *, retention_days: int) -> list[_DueMedia]:
    rows = await db.execute(
        sa_text(
            "SELECT id, dropbox_path, status FROM digest_hosted_media "
            "WHERE (status = :uploaded "
            "       AND uploaded_at < now() - make_interval(days => :days)) "
            "   OR (status = :pending "
            "       AND created_at < now() - make_interval(mins => :mins)) "
            "ORDER BY id ASC"
        ),
        {
            "uploaded": HostedMediaStatus.uploaded,
            "pending": HostedMediaStatus.pending,
            "days": retention_days,
            "mins": PENDING_RECONCILE_MINUTES,
        },
    )
    return [_DueMedia(id=row[0], dropbox_path=row[1], status=row[2]) for row in rows]


async def _stamp_swept(session_factory: SessionFactory, row: _DueMedia) -> None:
    """`swept_at` is the claim that the object IS gone, so it is stamped only after the remote
    delete (or the probe that found nothing) has actually answered. A CAS on the row's previous
    status keeps the transition write-once."""
    async with session_factory() as db:
        await db.execute(
            sa_text(
                "UPDATE digest_hosted_media SET status = :swept, swept_at = now() "
                "WHERE id = :id AND status = :previous"
            ),
            {"id": row.id, "swept": HostedMediaStatus.swept, "previous": row.status},
        )
        await db.commit()


async def _resolve_due(
    session_factory: SessionFactory, host: MediaHost, due: list[_DueMedia]
) -> int:
    """Delete or reconcile each due row. Returns how many objects are now gone from Dropbox."""
    swept = 0
    for row in due:
        try:
            if row.status == HostedMediaStatus.pending:
                if await _reconcile_pending(session_factory, host, row):
                    swept += 1
                continue
            await host.delete(row.dropbox_path)
        except (hosting.DropboxError, httpx.HTTPError):
            logger.warning("digest.retention_delete_failed", ledger_id=row.id)
            continue
        await _stamp_swept(session_factory, row)
        swept += 1
    return swept


async def _reconcile_pending(
    session_factory: SessionFactory, host: MediaHost, row: _DueMedia
) -> bool:
    """Ask Dropbox what happened to a stuck `pending` row. Returns whether it ended up `swept`."""
    if not await host.remote_exists(row.dropbox_path):
        await _stamp_swept(session_factory, row)
        logger.info("digest.hosted_media_reconciled", ledger_id=row.id, outcome="absent")
        return True
    link = await host.shared_link(row.dropbox_path)
    async with session_factory() as db:
        await db.execute(
            sa_text(
                "UPDATE digest_hosted_media SET status = :uploaded, shared_link = :link, "
                "uploaded_at = now() WHERE id = :id AND status = :pending"
            ),
            {
                "id": row.id,
                "link": link,
                "uploaded": HostedMediaStatus.uploaded,
                "pending": HostedMediaStatus.pending,
            },
        )
        await db.commit()
    logger.info("digest.hosted_media_reconciled", ledger_id=row.id, outcome="uploaded")
    return False


# --- the probes the admin panel drives ------------------------------------------------------------


async def preview(
    session_factory: SessionFactory = async_session, *, now_utc: datetime | None = None
) -> tuple[RenderedDigest, DigestConfig]:
    """Render what would go out now. Persists nothing and uploads nothing."""
    async with session_factory() as db:
        settings = await load_settings(db)
    moment = now_utc or datetime.now(UTC)
    selection = await selection_mod.select_for_run(session_factory, settings, moment)
    return await build_digest(session_factory, settings, selection, host=None), settings


async def test_send(
    session_factory: SessionFactory = async_session,
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

    Bounded on purpose — no uploads, one recipient — so it stays synchronous and answers the admin
    in the request that asked. Its database reads are still taken in short sessions that are
    CLOSED before SMTP is contacted: "bounded" is about how long the work takes, not a licence to
    hold a connection through it.
    """
    async with session_factory() as db:
        settings = await load_settings(db)
    moment = now_utc or datetime.now(UTC)
    active_transport = transport or default_transport()
    sender = sender_address(settings)
    if address.lower() == sender.lower():
        # The same refusal a real run applies (AC 7), and for the same reason: iCloud DROPS a
        # message whose From equals its To when it carries attachments, so the probe would report
        # a clean send and deliver nothing — the exact false-success this feature exists to end,
        # and worse here because a test-send is what an admin uses to CHECK the channel.
        raise SenderIsRecipient(ERROR_SENDER_IS_RECIPIENT)
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

    selection = await selection_mod.select_for_run(session_factory, settings, moment)
    rendered = await build_digest(session_factory, settings, selection, host=None)
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
