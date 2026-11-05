"""Deleting an attachment's bytes and its row, in the one order that cannot lose track of bytes.

THE PRINCIPLE: an attachment ROW is the durable journal for its bytes. Nothing else records where
an attachment's files are — the paths are derived from the row (`asset_paths.all_asset_paths`), so
a row that goes away takes the only map to its files with it.

Three rules follow, and every deleting path in this codebase obeys them:

1. Never delete a row while its bytes are still there.
2. Never unlink bytes while their row's transaction could still roll back.
3. Lock and REVALIDATE a row before acting on it — a row selected a moment ago may since have been
   claimed by a concurrent write.

Read together, they leave exactly two states after a crash at any point:

  (a) a row whose memo is gone — an orphan the reconcile predicate finds and can retry, or
  (b) a row whose files are already gone — a dangling row, harmless, cleaned on the next attempt.

The forbidden third state is bytes with NO row: nothing names them, no sweep can find them, and
they sit on the data volume for ever. That is what deleting a row before its files produces, and
it is why the ordering below looks inverted at first glance.

Rules 1 and 2 are not in tension, because they speak about different rows. Rule 2 protects a row
whose transaction is still open — a memo create that may yet roll back and hand its upload back to
the client (MEMNG-142: a deduped upload's part file is recorded and unlinked only after the commit
that consumed it). Rule 1 speaks about a row that is already committed as garbage: an orphaned
attachment nothing references. For THAT row, unlinking before the row delete is exactly right — a
crash lands in state (b) — while the reverse lands in the forbidden state.

THE ORDER, and why it is what it is:

    1. sweep this attachment's media_jobs rows (FOR UPDATE SKIP LOCKED)
    2. SELECT the attachment FOR UPDATE, and revalidate it
    3. collect the full derivable path set
    4. unlink the bytes, tolerating the ones that are not there
    5. delete the row
    6. commit

Jobs BEFORE the attachment row, not after: `media.guard_publish` locks job-then-attachment, so a
deleter that took them the other way round would deadlock against a publishing worker (the lock
order is the invariant MEMNG-90 left behind, documented in guard_publish, and the J → A direction
the whole protocol adopted — MEMNG-267 R7). SKIP LOCKED not because a worker sits on the row for
the length of a transcode — it does not, since MEMNG-90 commits the claim before processing
(`app/worker.py:279`) — but because a job row IS momentarily held by whichever short transaction
the worker is inside, and this sweep has no reason to wait for it: the row it skips belongs to a
run that will find the attachment gone and discard itself (MEMNG-41).

Step 4 skips any path ANOTHER attachment row still names (MEMNG-959, `_named_elsewhere`) — any of
the paths it is about to unlink, the derived tiers and print profiles included (MEMNG-1167). A
path is what two rows could share — identical bytes in two memos are two names of one inode (a
staging hit's hard link), each row holding its own, so that case never trips it; the check is the
guard for two rows pointed at one file, where unlinking for the first would take the second's
picture.
A skipped path is not leaked: a row still names it, and the last row to go unlinks it. Two
reclaims of such a pair are serialised on a transaction-scoped advisory lock per path, taken after
the row lock — otherwise each would see the other's row still there, both would keep the file,
and both rows would go.

Holding the attachment lock from step 2 to the commit is what closes the late-publish window: a
worker republishing a tier takes that same lock inside `guard_publish`, so it either finishes
before step 3 (and its file is in the set collected there) or waits until after step 6 (and finds
the row gone, and discards its own output). Collecting the DERIVABLE set rather than the files that
happen to exist is the other half of it — a path that is empty now may not be by step 4.
"""

import asyncio
import enum
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog
from anyio import to_thread
from sqlalchemy import ColumnElement, Select, exists, func, select, union
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentRendition,
    Collection,
    CollectionCover,
    CollectionMember,
    DigestHostedMedia,
    MediaJob,
    Upload,
    attachment_tags,
)
from app.services import printready, storage
from app.services.asset_paths import all_asset_paths

logger = structlog.get_logger(__name__)

# A file placed under `originals/YYYY/MM/<memo-folder>/` was put there by `link_attachments`, which
# only ever runs for an attachment being linked to a memo. So a row with NO memo whose original
# lives there is one a memo delete left behind — as opposed to a 0.29.1 migration orphan, whose
# original sits in the FLAT `originals/` bucket and which is legitimate media nobody's memo owns.
# Not anchored: the stored path is absolute and its prefix is the deployment's DATA_ROOT.
LEAKED_PATH_RE = r"originals/[0-9]{4}/[0-9]{2}/"
_LEAKED_PATH = re.compile(LEAKED_PATH_RE)

# A row a standalone finalize made (`POST /api/uploads/{uid}/finalize`) and no memo ever took
# (MEMNG-968). Counted once it is a day old, reclaimed once it is three days old, both by its
# `created_at` — the instant finalize inserted it (a twin-built row does not copy its twin's).
NEVER_LINKED_COUNT_AFTER = timedelta(hours=24)
NEVER_LINKED_RECLAIM_AFTER = timedelta(hours=72)


class Revalidate(enum.StrEnum):
    """WHAT the reclaim re-checks under the row lock — which is a different question per caller,
    and getting it wrong is how a reclaim either destroys live media or silently does nothing.

    The rule is: revalidate the premise the CALLER selected the row on, and nothing else.
    """

    IDENTITY = "identity"
    """The caller named this exact row and wants it gone whether or not a memo shows it —
    `DELETE /api/attachments/{uid}`. Its premise is 'this row exists', so that is the whole check;
    linkage is not a reason to refuse, it is the route's ordinary case."""

    ORPHANED = "orphaned"
    """The caller just orphaned this row itself and is cleaning up after its own commit — the memo
    delete. Its premise is 'no memo shows this any more', so a row that has since been claimed by a
    concurrent upload is live media and must be left alone."""

    DETACHED = "detached"
    """A periodic sweep picked this row because it has been detached from its memo for longer than
    its grace period. Age is the whole of what that establishes — nothing in it says the request
    that detached the row has finished cleaning up after its own commit — so the premise is the
    predicate itself, and the instant is re-read under the lock against the SAME cutoff the pass
    selected on: a row a memo linked since carries no instant, and a row detached again since — or
    adopted by a standalone finalize, which restarts the instant (MEMNG-1168) — carries a newer
    one, and each belongs to whoever touched it last."""

    LEAKED_ORPHAN = "leaked_orphan"
    """A blind scan picked this row BECAUSE it looked like garbage — the reconcile script. Its
    premise is both halves of that predicate, and its selection is the oldest thing anyone here
    acts on, so both are re-checked."""

    NEVER_LINKED = "never_linked"
    """The worker's never-linked sweep picked this row by `never_linked_clauses` and a creation
    cutoff (MEMNG-968). Its premise is the WHOLE of that predicate — the `_unfiled/` discriminator,
    every exclusion and the cutoff — so all of it is asked again under the row lock, in the same
    SQL, against the cutoff the pass selected on. Without a cutoff it refuses (fail closed)."""


Outcome = Literal["reclaimed", "vanished", "skipped_ineligible", "failed_unlink"]


@dataclass(frozen=True)
class ReclaimResult:
    """What one reclaim did. `failed_paths` is non-empty only for `failed_unlink`."""

    outcome: Outcome
    failed_paths: tuple[str, ...] = field(default=())


def never_linked_clauses() -> list[ColumnElement[bool]]:
    """What makes a row "finalized by an upload session and never linked" (MEMNG-968), as SQL —
    the sweep's selection, the gauge's count and the NEVER_LINKED revalidation all read THIS list.

    `memo_id IS NULL` alone is the wrong predicate: the 0.29.1 imports (flat `originals/`) and the
    leftovers of old memo deletes (dated memo folders) carry it too and are household archive
    media. The positive discriminator is `storage.is_unfiled`: only a standalone finalize leaves an
    attachment's original DIRECTLY in `<DATA_ROOT>/originals/_unfiled/`, because `link_attachments`
    moves it out the moment a memo takes it. Compared as a literal prefix plus "no further slash",
    never as a LIKE pattern, whose `_` would match any character. `detached_at IS NULL` leaves a
    detached row to the detached sweep, which judges it by its own clock.

    Then what keeps a never-linked row: a same-owner upload session declaring the same bytes (it
    may be about to stage onto or dedup into this row, MEMNG-959 — the finalized row's own session
    is gone, finalize deletes it), and anything the owner did with the picture the library shows
    them — an album, a cover, a picture tag, a print rendition, a digest's hosted copy.
    """
    prefix = str(storage.unfiled_dir(Path(settings.DATA_ROOT))) + "/"
    return [
        Attachment.memo_id.is_(None),
        Attachment.detached_at.is_(None),
        func.left(Attachment.storage_path, len(prefix)) == prefix,
        func.strpos(func.substr(Attachment.storage_path, len(prefix) + 1), "/") == 0,
        Attachment.print_path.is_(None),
        ~exists().where(Upload.owner_id == Attachment.owner_id, Upload.sha256 == Attachment.sha256),
        ~exists().where(CollectionMember.attachment_id == Attachment.id),
        ~exists().where(CollectionCover.attachment_id == Attachment.id),
        ~exists().where(Collection.cover_attachment_id == Attachment.id),
        ~exists().where(attachment_tags.c.attachment_id == Attachment.id),
        ~exists().where(AttachmentRendition.attachment_id == Attachment.id),
        ~exists().where(DigestHostedMedia.attachment_id == Attachment.id),
    ]


async def _still_never_linked(db: AsyncSession, att_id: int, created_before: datetime) -> bool:
    """NEVER_LINKED's revalidation: the same clauses, asked of this one row, under its lock.

    Every exclusion table carries a foreign key to `attachments`, so an insert naming this row
    takes a KEY SHARE lock on it: one already in flight makes our FOR UPDATE wait for its commit
    (and this READ COMMITTED statement then sees it), and one that starts after waits for ours and
    finds the row gone. The upload session is not a foreign key; one that opens after this check
    stages a hard link of its own (MEMNG-959) or finalizes into a fresh row, so no bytes are lost.
    """
    found = await db.execute(
        select(Attachment.id).where(
            Attachment.id == att_id,
            Attachment.created_at <= created_before,
            *never_linked_clauses(),
        )
    )
    return found.scalar_one_or_none() is not None


def leaked_orphans_stmt() -> Select[tuple[Attachment]]:
    """Rows no memo points at whose original sits UNDER A DATED MEMO FOLDER — the reconcile
    script's predicate, and nobody else's. A deleted memo is the ordinary way a row gets here, but
    it is not the only one: a PATCH whose own reclaim failed leaves a matching row behind while its
    memo goes on living. What the predicate asserts is just those two things. An original imported
    from 0.29.1 stays flat in `originals/`, so this path test cannot see a detached import at
    all."""
    return (
        select(Attachment)
        .where(Attachment.memo_id.is_(None), Attachment.storage_path.op("~")(LEAKED_PATH_RE))
        .order_by(Attachment.id)
    )


def unlink_paths(paths: list[Path]) -> list[str]:
    """Remove every one of these files; returns the ones that could not be removed.

    Absent is success (`missing_ok`): the set is derivable rather than observed, so most of it will
    not exist for any given attachment, and a retry of an interrupted reclaim finds the rest
    already gone. Anything this DOES return is therefore a real failure — a read-only mount, a
    permission problem, an I/O error — never a file that was simply not there.
    """
    failed: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))
    return failed


def _candidate_paths(paths: list[Path]) -> list[str]:
    """EVERY file this reclaim is about to unlink, as the strings a column would hold, sorted (the
    lock order). Not just the ones the row names in a column (MEMNG-1167): a derived tier or an
    enumerated print profile is keyed by this row's uid, but nothing stops another row's rendition
    or print path from naming that very file, and unlinking it would take that row's picture."""
    return sorted({str(path) for path in paths})


def _path_lock_stmt(path: str):
    """The per-path advisory lock two reclaims of a shared file serialise on (released at commit or
    rollback). Taken in sorted path order, after the row lock; nothing else takes it."""
    return select(func.pg_advisory_xact_lock(func.hashtextextended(f"reclaim-path:{path}", 0)))


def _named_elsewhere_stmt(att_id: int, named: list[str]):
    """Which of `named` some OTHER attachment row — or another row's rendition — still names."""
    return union(
        select(Attachment.storage_path).where(
            Attachment.id != att_id, Attachment.storage_path.in_(named)
        ),
        select(Attachment.print_path).where(
            Attachment.id != att_id, Attachment.print_path.in_(named)
        ),
        select(AttachmentRendition.storage_path).where(
            AttachmentRendition.attachment_id != att_id, AttachmentRendition.storage_path.in_(named)
        ),
    )


async def _named_elsewhere(db: AsyncSession, att: Attachment, paths: list[Path]) -> frozenset[str]:
    """The reference count (MEMNG-959): which of `paths` — every file this reclaim would unlink
    (MEMNG-1167) — another row still names, which this reclaim must leave on disk. Asked under the
    per-path locks, so a concurrent reclaim of the other row has either committed its delete (and
    is not counted) or waits for this one."""
    named = _candidate_paths(paths)
    if not named:
        return frozenset()
    for path in named:
        await db.execute(_path_lock_stmt(path))
    rows = await db.execute(_named_elsewhere_stmt(att.id, named))
    return frozenset(path for (path,) in rows if path)


def _named_elsewhere_sync(session: Session, att: Attachment, paths: list[Path]) -> frozenset[str]:
    """`_named_elsewhere` for the maintenance script's synchronous reclaim."""
    named = _candidate_paths(paths)
    if not named:
        return frozenset()
    for path in named:
        session.execute(_path_lock_stmt(path))
    rows = session.execute(_named_elsewhere_stmt(att.id, named))
    return frozenset(path for (path,) in rows if path)


def _unshared(paths: list[Path], shared: frozenset[str], att: Attachment) -> list[Path]:
    if shared:
        logger.info(
            "reclaim.shared_path_kept", attachment_id=att.id, uid=att.uid, paths=sorted(shared)
        )
    return [path for path in paths if str(path) not in shared]


def _has_leaked_path(att: Attachment) -> bool:
    return att.storage_path is not None and _LEAKED_PATH.search(att.storage_path) is not None


def _ineligible(
    att: Attachment,
    mode: Revalidate,
    *,
    detached_at: datetime | None = None,
    detached_before: datetime | None = None,
    created_before: datetime | None = None,
) -> str | None:
    """Why this row may no longer be reclaimed under `mode`, or None to go ahead.

    `detached_at` is ORPHANED's fence and `detached_before` is DETACHED's cutoff; each mode reads
    only its own, because the two ask different questions of the same column — "is this still the
    detachment I performed?" against "is this still older than the pass's cutoff?".
    """
    if mode is Revalidate.IDENTITY:
        return None  # the row was named, and re-reading it under the lock IS the revalidation
    if att.memo_id is not None:
        return "relinked"
    if mode is Revalidate.LEAKED_ORPHAN and not _has_leaked_path(att):
        return "path_left_the_predicate"
    if mode is Revalidate.ORPHANED and detached_at is not None:
        # The epoch fence: this reclaim was issued ABOUT one detachment, and the row must still
        # carry it. A newer instant means a standalone finalize adopted the row (it restarts the
        # clock) or something detached it again, and either owns it now; NULL means a memo took
        # it. A caller that names no instant is asking the older question — "is this row still
        # unlinked?" — and gets the older answer.
        if att.detached_at != detached_at:
            return "adopted_or_redetached"
    if mode is Revalidate.DETACHED:
        # The same fence, against a pass instead of a transaction — and fail closed without a
        # cutoff, because "sweep every detached row, whenever it was detached" is the one reading
        # of this mode that deletes a picture the user is still editing.
        if detached_before is None or att.detached_at is None or att.detached_at > detached_before:
            return "adopted_or_redetached"
    if mode is Revalidate.NEVER_LINKED and created_before is None:
        # Fail closed, as DETACHED does: "every never-linked row, whatever its age" would take a
        # picture whose memo is still being composed. The rest of the premise is SQL — see
        # `_still_never_linked`, which `reclaim_attachment` asks next.
        return "no_cutoff"
    return None


async def _unlink_off_the_loop(
    remove: Callable[[list[Path]], list[str]], paths: list[Path]
) -> tuple[list[str], asyncio.CancelledError | None]:
    """Unlink OFF the event loop, and come back only once the thread has RETURNED.

    A cancellation delivered to the reclaim itself — `asyncio.run`'s teardown of whatever tasks are
    left, the API loop's own, a caller cancelling the task directly — arrives at the `await` on
    this thread AT ONCE, while the thread runs on: nothing about a worker thread can be cancelled.
    Taken there, the reclaim would unwind mid-unlink, which is precisely the state this module
    exists to prevent. So the thread is started once, and each cancellation is caught, REMEMBERED
    and the SAME future awaited again — the shield is what keeps the re-await from passing the
    cancellation down to it — until the unlinks are actually over. EVERY cancellation, with no
    case for a future that is already done: nothing cancels this one (it is deliberately not a
    task), so done at a cancellation means the offload FINISHED, its answer or its error sitting
    in it, and the re-await hands that back at once instead of unwinding on top of it. The
    remembered cancellation is handed back for the caller to re-raise after the transaction.

    `run_in_executor` rather than this module's usual `to_thread.run_sync`, for the one reason that
    decides it: a coroutine has to be driven by a TASK, and a task is exactly what the teardown
    cancels — `asyncio.run` cancels every task `all_tasks()` returns, this offload's included, and
    a cancelled offload loses the thread's answer while the thread keeps deleting. The future this
    returns is not a task, so the teardown leaves it alone and the answer still arrives.

    The profile enumeration above keeps `to_thread.run_sync` and stays cancellable on purpose: it
    only reads, so a reclaim that ends there has deleted nothing and rolls back with every file
    still named by its row.
    """
    thread = asyncio.get_running_loop().run_in_executor(None, remove, paths)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(thread), cancellation
        except asyncio.CancelledError as exc:
            cancellation = exc


async def reclaim_attachment(
    db: AsyncSession,
    attachment_id: int,
    mode: Revalidate,
    *,
    unlink: Callable[[list[Path]], list[str]] | None = None,
    detached_at: datetime | None = None,
    detached_before: datetime | None = None,
    created_before: datetime | None = None,
) -> ReclaimResult:
    """Reclaim ONE attachment in its own transaction, in the order this module documents.

    The caller commits nothing; this commits its own transaction, because the row lock it takes has
    to be released before the next row is touched.

    `detached_at` and `detached_before` carry the caller's premise about WHEN this row was detached
    — the instant it detached it with, or the cutoff its pass selected on — so the revalidation
    under the lock can be about the same instant the decision was taken about (see `_ineligible`).
    `created_before` is the never-linked sweep's cutoff, NEVER_LINKED's alone.

    `unlink` is the seam the crash-window tests inject at; production passes nothing. It is called
    OFF the event loop, in a worker thread this coroutine then waits out whatever happens to it
    (`_unlink_off_the_loop`), so a seam that touches the loop is a test that deadlocks itself.
    """
    remove = unlink or unlink_paths
    # 1. Jobs first — the lock order guard_publish depends on. SKIP LOCKED: a running transcode's
    #    row belongs to its worker, which discards its own output on finding the attachment gone.
    unlocked_jobs = (
        select(MediaJob.id)
        .where(MediaJob.attachment_id == attachment_id)
        .with_for_update(skip_locked=True)
    )
    await db.execute(
        sa_delete(MediaJob)
        .where(MediaJob.id.in_(unlocked_jobs))
        .execution_options(synchronize_session=False)
    )
    # 2. …then the attachment row, locked and revalidated against the caller's own premise.
    #    `populate_existing`: the caller's session has usually already loaded this row — the memo
    #    delete nulls the FK on its loaded attachments and the sessionmaker does not expire on
    #    commit — so without it the lock is taken in the database while `_ineligible` decides from
    #    the identity map, about a `memo_id` that has passed. A relink committed in between would be
    #    deleted anyway (MEMNG-297). Reading the row's CURRENT state under the lock IS the
    #    revalidation.
    att = (
        await db.execute(
            select(Attachment)
            .where(Attachment.id == attachment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if att is None:
        await db.commit()
        return ReclaimResult("vanished")
    ineligible = _ineligible(
        att,
        mode,
        detached_at=detached_at,
        detached_before=detached_before,
        created_before=created_before,
    )
    if (
        ineligible is None
        and mode is Revalidate.NEVER_LINKED
        and created_before is not None
        and not await _still_never_linked(db, att.id, created_before)
    ):
        ineligible = "left_the_never_linked_predicate"
    if ineligible is not None:
        uid = att.uid  # read BEFORE the rollback: it expires every instance, and reading an
        # expired column would try to refresh it — blocking IO from a place the async session
        # cannot await, i.e. MissingGreenlet instead of the warning this line is meant to emit.
        await db.rollback()  # give the job rows back too: this attachment is live again
        logger.warning("reclaim.skipped", attachment_id=attachment_id, uid=uid, reason=ineligible)
        return ReclaimResult("skipped_ineligible")
    # 3-5. The derivable set PLUS the print profiles that are actually on disk, unlinked, and only
    #      then the row. The profiles have to be enumerated rather than derived (MEMNG-238: the set
    #      of profiles that ever existed is unbounded), and both the listing and the unlinks go off
    #      the event loop: every stat and every unlink on this volume is a network round-trip. The
    #      loop they would otherwise hold is the worker's, which stamps the heartbeat and drains
    #      media, or — for PATCH and DELETE, which run in the API process — the one serving every
    #      other request.
    uid = att.uid  # read BEFORE any rollback below: it expires every instance (see above)
    try:
        profile_assets = await to_thread.run_sync(printready.profile_variant_paths_sync, uid)
    except OSError as exc:
        # FAIL CLOSED, for the same reason a failed unlink does. The enumeration could not be
        # ESTABLISHED — an unreadable mount, an I/O error — so we do not know what this row owns,
        # and deleting it now would leave whatever is in those directories with nothing naming it.
        # The row stays and the whole transaction unwinds; a later pass retries it, and by then the
        # mount is either back or the problem is visible. That pass is the worker's detached-row
        # sweep (`worker.sweep_detached_attachments`) once the row's `detached_at` is past the
        # grace — not the commit-race reconcile, which only selects rows that still have a memo. A
        # row whose `detached_at` is NULL is invisible to that sweep too: only the reconcile
        # script finds it, and only while its original sits under a dated memo folder
        # (`leaked_orphans_stmt`).
        await db.rollback()
        logger.warning(
            "reclaim.profile_scan_failed", attachment_id=attachment_id, uid=uid, error=str(exc)
        )
        return ReclaimResult("failed_unlink", (str(printready.printready_path(uid).parent),))
    # 4. …minus whatever another row still names: that file is the other row's, not garbage.
    candidates = all_asset_paths(att) + profile_assets
    shared = await _named_elsewhere(db, att, candidates)
    paths = _unshared(candidates, shared, att)
    failed, cancellation = await _unlink_off_the_loop(remove, paths)
    if failed:
        # FAIL CLOSED. Deleting the row now would leave these files with nothing naming them — the
        # one outcome this module exists to prevent, arriving on the very failure the journal is
        # for. The row stays, the whole transaction unwinds, and a retry re-derives the full set
        # and tolerates the ones this attempt already removed.
        await db.rollback()
        logger.error("reclaim.unlink_failed", attachment_id=attachment_id, paths=failed)
        result = ReclaimResult("failed_unlink", tuple(failed))
    else:
        await db.delete(att)
        await db.commit()
        result = ReclaimResult("reclaimed")
    if cancellation is not None:
        # Only NOW, with the unlinks over: the row's lock outlived the thread doing them, which is
        # the guarantee this holds a cancellation back for. Unwinding at the await above would have
        # released that lock with the unlinking still going — the row live again, adoptable, and a
        # thread deleting the adopter's bytes. The TRANSACTION is not guaranteed with it and is
        # deliberately not shielded: a cancellation that first arrives on the `db.delete` or the
        # `db.commit` just above — the thread has long returned by then — interrupts it, and what
        # the database did with it is then unknown here: the COMMIT may have completed before the
        # interruption reached this side, or never have been sent. So either the row is gone with
        # its files, or it stays naming files that are gone. That second state is the recoverable
        # half of the trade, and the reason the row is the journal. The cancellation is not
        # swallowed either: whoever asked for it still gets it, one step later than they asked.
        raise cancellation
    return result


# Docker allows 10 s between SIGTERM and SIGKILL (the stack compose sets no `stop_grace_period`),
# and that is the only outer bound there is: neither entrypoint configures a graceful-shutdown
# timeout of its own. It is the whole stop's budget, not this step's alone, so what it bounds here
# is how long a shutdown KEEPS WAITING before it says what is still running — never the reclaim
# itself, which is exactly what must not be cut short.
DRAIN_GRACE_SECONDS = 10.0

# Every reclaim this process has in flight, whoever started it: the memo edit and delete routes,
# the asset delete route and the worker's detached sweep all go through `reclaim_row_shielded`,
# which is what puts a reclaim in here. A task is added when it is created and discards itself when
# it ends, so what is left at shutdown is precisely the work no caller is waiting for any more —
# the sweep whose bound expired, the request whose caller went away — and it is the only handle a
# shutdown has on it.
_pending: set[asyncio.Task[ReclaimResult]] = set()


async def reclaim_row(
    attachment_id: int,
    mode: Revalidate,
    *,
    unlink: Callable[[list[Path]], list[str]] | None = None,
    detached_at: datetime | None = None,
    detached_before: datetime | None = None,
    created_before: datetime | None = None,
) -> ReclaimResult:
    """ONE row's reclaim, session and all, as one inseparable unit — what every caller runs.

    The session is opened HERE rather than handed in, so that the lock, the unlink and the
    commit-or-rollback belong to the same thing: everything this row holds is released by this
    coroutine finishing. A caller's own session cannot release the row early, because the reclaim
    does not use one.
    """
    async with async_session() as db:
        return await reclaim_attachment(
            db,
            attachment_id,
            mode,
            unlink=unlink,
            detached_at=detached_at,
            detached_before=detached_before,
            created_before=created_before,
        )


async def reclaim_row_shielded(
    attachment_id: int,
    mode: Revalidate,
    *,
    unlink: Callable[[list[Path]], list[str]] | None = None,
    detached_at: datetime | None = None,
    detached_before: datetime | None = None,
    created_before: datetime | None = None,
) -> ReclaimResult:
    """`reclaim_row` as a TASK of its own, awaited through a shield — how every caller calls it.

    Every one of them: the memo edit and delete routes, the asset delete route, the detached sweep.
    Nothing else in this process reclaims a row.

    What keeps the row's lock alive through its unlinking is not the task: a cancellation reaching
    the reclaim is held until the unlink thread has RETURNED (`_unlink_off_the_loop`), and it is
    held the same way for a caller that awaits the unit inline. The task and its shield buy two
    other things. The CALLER stops where it was cancelled instead of waiting out a thread it is no
    longer there for — the shield puts its cancellation on this await rather than on the reclaim.
    And the reclaim is REGISTERED in `_pending`, so a shutdown's drain knows there is something to
    wait for (`drain_pending_reclaims`) and can wait for it after the request that started it is
    gone. So the lock outlives the thread in all three cases: the caller cancelled, the drain's
    grace expired, the teardown cancelling what is left. The transaction is not promised with it —
    a cancellation first arriving on the delete or the commit that follows interrupts it, and its
    outcome is then uncertain: committed, or the row left naming files that are already gone.

    The cancellation is then RE-RAISED, never swallowed: the caller was cancelled and is over, and
    a loop that went on to its next row would be taking rows nobody is waiting for with a
    cancellation pending. The reclaim already in flight is the one thing that continues, and it is
    the shutdowns' to await (`drain_pending_reclaims`), not this caller's.
    """
    task = asyncio.create_task(
        reclaim_row(
            attachment_id,
            mode,
            unlink=unlink,
            detached_at=detached_at,
            detached_before=detached_before,
            created_before=created_before,
        )
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.info("reclaim.caller_cancelled", attachment_id=attachment_id)
        raise


async def drain_pending_reclaims(grace_s: float = DRAIN_GRACE_SECONDS) -> None:
    """Wait for the reclaims still in flight in this process — the last thing either process does.

    Nothing is cancelled here, however long it takes: cancelling a reclaim mid-unlink is the very
    defect the unit above exists to prevent, and it would be no less one for being done by a
    shutdown. The grace bounds THIS WAIT and nothing else. Past it the wait simply ends and says
    what is still running — and what it leaves behind still finishes: the teardown that follows
    (`asyncio.run`'s in the worker, the loop's own in the API) cancels that task, and the reclaim
    holds the cancellation until its unlink thread has returned, inside the gather the teardown
    does after cancelling. Its transaction is the weaker half: a cancellation first arriving on the
    delete or the commit that follows interrupts it — committed or not, the row may be left naming
    files that are gone. What ends a
    reclaim mid-unlink is the process dying outright — SIGKILL at the end of the stop grace — and
    it ends it the hard way: the connection dies with it, the transaction rolls back, and the row
    is live again with part of its files gone.

    What finds such a row depends on its state, not on which caller left it. A DETACHED row —
    `memo_id` NULL with a `detached_at` stamp — is what the sweep selects
    (`worker.sweep_detached_attachments`), once that stamp is older than the sweep's grace; a pass
    can still leave it, on an unlink that fails again, and a later one takes it up. A LINKED row —
    which the asset-delete route can leave, since it reclaims any attachment its owner names,
    stamped or not — is selected by no sweep's predicate: it stays on its memo, listed, with its
    files gone, until the caller deletes that attachment again — a reclaim of the same row over a
    set that is mostly deleted already, and `unlink_paths` takes an absent file for success
    (`missing_ok`). Either way the row
    is the journal and the files are not.
    """
    pending = tuple(_pending)
    if not pending:
        return
    logger.info("reclaim_drain_waiting", count=len(pending))
    _, still_running = await asyncio.wait(pending, timeout=grace_s)
    if still_running:
        logger.warning("reclaim_drain_timeout", count=len(still_running))


def reclaim_attachment_sync(
    session: Session,
    attachment_id: int,
    mode: Revalidate,
    *,
    unlink: Callable[[list[Path]], list[str]] | None = None,
) -> ReclaimResult:
    """The maintenance script's reclaim: same order, same guarantees, a synchronous Session.

    Deliberately a second implementation rather than a shared one — the script runs against a
    psycopg2 DSN it is handed explicitly, and bridging that to the app's async engine would give a
    row-deleting tool a second way to pick its database. The ORDER is the thing that must not
    diverge, so the two live side by side here; the crash-window tests drive both.
    """
    remove = unlink or unlink_paths
    unlocked_jobs = (
        select(MediaJob.id)
        .where(MediaJob.attachment_id == attachment_id)
        .with_for_update(skip_locked=True)
    )
    session.execute(
        sa_delete(MediaJob)
        .where(MediaJob.id.in_(unlocked_jobs))
        .execution_options(synchronize_session=False)
    )
    att = session.execute(
        select(Attachment)
        .where(Attachment.id == attachment_id)
        .with_for_update()
        .execution_options(populate_existing=True)  # current state under the lock — see above
    ).scalar_one_or_none()
    if att is None:
        session.commit()
        return ReclaimResult("vanished")
    ineligible = _ineligible(att, mode)
    if ineligible is not None:
        session.rollback()
        return ReclaimResult("skipped_ineligible")
    uid = att.uid
    try:
        profile_assets = printready.profile_variant_paths_sync(uid)
    except OSError as exc:
        session.rollback()  # fail closed — see reclaim_attachment
        logger.warning(
            "reclaim.profile_scan_failed", attachment_id=attachment_id, uid=uid, error=str(exc)
        )
        return ReclaimResult("failed_unlink", (str(printready.printready_path(uid).parent),))
    candidates = all_asset_paths(att) + profile_assets
    shared = _named_elsewhere_sync(session, att, candidates)  # the reference count — see above
    paths = _unshared(candidates, shared, att)
    failed = remove(paths)
    if failed:
        session.rollback()  # fail closed — see reclaim_attachment
        logger.error("reclaim.unlink_failed", attachment_id=attachment_id, paths=failed)
        return ReclaimResult("failed_unlink", tuple(failed))
    session.delete(att)
    session.commit()
    return ReclaimResult("reclaimed")
