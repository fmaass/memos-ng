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

Holding the attachment lock from step 2 to the commit is what closes the late-publish window: a
worker republishing a tier takes that same lock inside `guard_publish`, so it either finishes
before step 3 (and its file is in the set collected there) or waits until after step 6 (and finds
the row gone, and discards its own output). Collecting the DERIVABLE set rather than the files that
happen to exist is the other half of it — a path that is empty now may not be by step 4.
"""

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog
from anyio import to_thread
from sqlalchemy import Select, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Attachment, MediaJob
from app.services import printready
from app.services.asset_paths import all_asset_paths

logger = structlog.get_logger(__name__)

# A file placed under `originals/YYYY/MM/<memo-folder>/` was put there by `link_attachments`, which
# only ever runs for an attachment being linked to a memo. So a row with NO memo whose original
# lives there is one a memo delete left behind — as opposed to a 0.29.1 migration orphan, whose
# original sits in the FLAT `originals/` bucket and which is legitimate media nobody's memo owns.
# Not anchored: the stored path is absolute and its prefix is the deployment's DATA_ROOT.
LEAKED_PATH_RE = r"originals/[0-9]{4}/[0-9]{2}/"
_LEAKED_PATH = re.compile(LEAKED_PATH_RE)


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

    LEAKED_ORPHAN = "leaked_orphan"
    """A blind scan picked this row BECAUSE it looked like garbage — the reconcile script. Its
    premise is both halves of that predicate, and its selection is the oldest thing anyone here
    acts on, so both are re-checked."""


Outcome = Literal["reclaimed", "vanished", "skipped_ineligible", "failed_unlink"]


@dataclass(frozen=True)
class ReclaimResult:
    """What one reclaim did. `failed_paths` is non-empty only for `failed_unlink`."""

    outcome: Outcome
    failed_paths: tuple[str, ...] = field(default=())


def leaked_orphans_stmt() -> Select[tuple[Attachment]]:
    """The attachments a deleted memo left behind, and only those — one definition, shared by the
    worker sweep and the reconcile script so the two can never disagree about what is garbage."""
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


def _has_leaked_path(att: Attachment) -> bool:
    return att.storage_path is not None and _LEAKED_PATH.search(att.storage_path) is not None


def _ineligible(att: Attachment, mode: Revalidate) -> str | None:
    """Why this row may no longer be reclaimed under `mode`, or None to go ahead."""
    if mode is Revalidate.IDENTITY:
        return None  # the row was named, and re-reading it under the lock IS the revalidation
    if att.memo_id is not None:
        return "relinked"
    if mode is Revalidate.LEAKED_ORPHAN and not _has_leaked_path(att):
        return "path_left_the_predicate"
    return None


async def reclaim_attachment(
    db: AsyncSession,
    attachment_id: int,
    mode: Revalidate,
    *,
    unlink: Callable[[list[Path]], list[str]] | None = None,
) -> ReclaimResult:
    """Reclaim ONE attachment in its own transaction, in the order this module documents.

    The caller commits nothing; this commits its own transaction, because the row lock it takes has
    to be released before the next row is touched.

    `unlink` is the seam the crash-window tests inject at; production passes nothing.
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
    att = (
        await db.execute(
            select(Attachment).where(Attachment.id == attachment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if att is None:
        await db.commit()
        return ReclaimResult("vanished")
    ineligible = _ineligible(att, mode)
    if ineligible is not None:
        uid = att.uid  # read BEFORE the rollback: it expires every instance, and reading an
        # expired column would try to refresh it — blocking IO from a place the async session
        # cannot await, i.e. MissingGreenlet instead of the warning this line is meant to emit.
        await db.rollback()  # give the job rows back too: this attachment is live again
        logger.warning("reclaim.skipped", attachment_id=attachment_id, uid=uid, reason=ineligible)
        return ReclaimResult("skipped_ineligible")
    # 3-5. The derivable set PLUS the print profiles that are actually on disk, unlinked, and only
    #      then the row. The profiles have to be enumerated rather than derived (MEMNG-238: the set
    #      of profiles that ever existed is unbounded), and this runs on the request loop, so the
    #      one directory listing goes off it — every stat on this volume is a network round-trip.
    uid = att.uid  # read BEFORE any rollback below: it expires every instance (see above)
    try:
        profile_assets = await to_thread.run_sync(printready.profile_variant_paths_sync, uid)
    except OSError as exc:
        # FAIL CLOSED, for the same reason a failed unlink does. The enumeration could not be
        # ESTABLISHED — an unreadable mount, an I/O error — so we do not know what this row owns,
        # and deleting it now would leave whatever is in those directories with nothing naming it.
        # The row stays and the whole transaction unwinds; the caller retries (the reconcile sweep
        # finds it again), and by then the mount is either back or the problem is visible.
        await db.rollback()
        logger.warning(
            "reclaim.profile_scan_failed", attachment_id=attachment_id, uid=uid, error=str(exc)
        )
        return ReclaimResult("failed_unlink", (str(printready.printready_path(uid).parent),))
    paths = all_asset_paths(att) + profile_assets
    failed = remove(paths)
    if failed:
        # FAIL CLOSED. Deleting the row now would leave these files with nothing naming them — the
        # one outcome this module exists to prevent, arriving on the very failure the journal is
        # for. The row stays, the whole transaction unwinds, and a retry re-derives the full set
        # and tolerates the ones this attempt already removed.
        await db.rollback()
        logger.error("reclaim.unlink_failed", attachment_id=attachment_id, paths=failed)
        return ReclaimResult("failed_unlink", tuple(failed))
    await db.delete(att)
    await db.commit()
    return ReclaimResult("reclaimed")


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
        select(Attachment).where(Attachment.id == attachment_id).with_for_update()
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
    paths = all_asset_paths(att) + profile_assets
    failed = remove(paths)
    if failed:
        session.rollback()  # fail closed — see reclaim_attachment
        logger.error("reclaim.unlink_failed", attachment_id=attachment_id, paths=failed)
        return ReclaimResult("failed_unlink", tuple(failed))
    session.delete(att)
    session.commit()
    return ReclaimResult("reclaimed")
