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
order is the invariant MEMNG-90 left behind, documented in guard_publish). SKIP LOCKED because a
worker holds its claimed job's row for the whole run and waiting on a slow-lane transcode would pin
a request-serving connection for minutes (MEMNG-41).

Holding the attachment lock from step 2 to the commit is what closes the late-publish window: a
worker republishing a tier takes that same lock inside `guard_publish`, so it either finishes
before step 3 (and its file is in the set collected there) or waits until after step 6 (and finds
the row gone, and discards its own output). Collecting the DERIVABLE set rather than the files that
happen to exist is the other half of it — a path that is empty now may not be by step 4.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import structlog
from sqlalchemy import Select, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Attachment, MediaJob
from app.services.asset_paths import all_asset_paths

logger = structlog.get_logger(__name__)

# A file placed under `originals/YYYY/MM/<memo-folder>/` was put there by `link_attachments`, which
# only ever runs for an attachment being linked to a memo. So a row with NO memo whose original
# lives there is one a memo delete left behind — as opposed to a 0.29.1 migration orphan, whose
# original sits in the FLAT `originals/` bucket and which is legitimate media nobody's memo owns.
# Not anchored: the stored path is absolute and its prefix is the deployment's DATA_ROOT.
LEAKED_PATH_RE = r"originals/[0-9]{4}/[0-9]{2}/"
_LEAKED_PATH = re.compile(LEAKED_PATH_RE)

Outcome = Literal["reclaimed", "vanished", "skipped_relinked"]


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

    Absent is success: the set is derivable rather than observed, so most of it will not exist for
    any given attachment, and a retry of an interrupted reclaim finds the rest already gone.
    """
    failed: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))
    return failed


def _relinked(att: Attachment, require_leaked_path: bool) -> bool:
    """Whether this row stopped being garbage since it was selected.

    `memo_id` is the whole test for a caller reclaiming rows it has just orphaned itself. A blind
    scan revalidates the path predicate too: the row it picked may have been re-linked AND placed
    into a memo folder in the meantime, and the selection it acted on is that much older.
    """
    if att.memo_id is not None:
        return True
    return require_leaked_path and not _has_leaked_path(att)


def _has_leaked_path(att: Attachment) -> bool:
    return att.storage_path is not None and _LEAKED_PATH.search(att.storage_path) is not None


async def reclaim_attachment(
    db: AsyncSession,
    attachment_id: int,
    *,
    require_leaked_path: bool = False,
    unlink: Callable[[list[Path]], list[str]] | None = None,
) -> Outcome:
    """Reclaim ONE orphaned attachment in its own transaction, in the order this module documents.

    Returns what happened: `reclaimed`, `vanished` (someone else got there first), or
    `skipped_relinked` (a concurrent write claimed it — its bytes belong to a live memo now and
    MUST NOT be touched). The caller commits nothing; this commits its own transaction, because the
    lock it holds has to be released before the next row is taken.

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
    # 2. …then the attachment row, locked and revalidated. Both halves matter: the lock is what
    #    makes a publishing worker wait, and the revalidation is what keeps a row that has since
    #    been claimed by a concurrent upload from having its live memo's media deleted.
    att = (
        await db.execute(
            select(Attachment).where(Attachment.id == attachment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if att is None:
        await db.commit()
        return "vanished"
    if _relinked(att, require_leaked_path):
        await db.rollback()  # give the job rows back too: this attachment is live again
        logger.warning("reclaim.skipped_relinked", attachment_id=attachment_id, uid=att.uid)
        return "skipped_relinked"
    # 3-5. The derivable set, unlinked, and only then the row. A crash between 4 and 5 leaves a
    #      dangling row — state (b) — which the next sweep finishes off.
    paths = all_asset_paths(att)
    failed = remove(paths)
    if failed:
        logger.error("reclaim.unlink_failed", attachment_id=attachment_id, paths=failed)
    await db.delete(att)
    await db.commit()
    return "reclaimed"


def reclaim_attachment_sync(
    session: Session,
    attachment_id: int,
    *,
    require_leaked_path: bool = True,
    unlink: Callable[[list[Path]], list[str]] | None = None,
) -> Outcome:
    """The maintenance script's reclaim: same order, same guarantees, a synchronous Session.

    Deliberately a second implementation rather than a shared one — the script runs against a
    psycopg2 DSN it is handed explicitly, and bridging that to the app's async engine would give a
    row-deleting tool a second way to pick its database. The ORDER is the thing that must not
    diverge, so the two live side by side here; a test drives both through the same crash windows.
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
        return "vanished"
    if _relinked(att, require_leaked_path):
        session.rollback()
        return "skipped_relinked"
    paths = all_asset_paths(att)
    failed = remove(paths)
    if failed:
        logger.error("reclaim.unlink_failed", attachment_id=attachment_id, paths=failed)
    session.delete(att)
    session.commit()
    return "reclaimed"
