"""The "memo is live" push (MEMNG-967): ONE push per memo, per recipient, once no file of the memo
is still in flight.

When: the moment no attachment of the memo is `uploading`, `pending` or `processing`. A `failed`
file is terminal too — the worker has exhausted its retries, the memo is on the timeline with that
tile, and the owner already got the per-file "failed" push; waiting for it would leave the memo
unannounced for ever. A memo with no files is live at create.

Who: the owner, always; and every OTHER user with `notify_on_household_posts` on — but only when
the memo is PROTECTED or PUBLIC when the push is sent. A PRIVATE memo reaches nobody but its owner.

Exactly once: `claim` is one conditional UPDATE that stamps `memos.live_notified_at` only while it
is NULL and no file is in flight, and RETURNs the row only to the transaction that stamped it. Two
consumers finishing the memo's last two files race onto the same row lock; the second re-checks the
predicate against the first one's committed stamp and matches nothing. Each caller claims AFTER its
own `ready` commit, so whichever finishes last always sees every file terminal — the memo cannot be
missed either. The claim commits before anything is sent: a crash between the two loses the push
rather than doubling it (at most once, and a push is best-effort anyway).
"""

import structlog
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Attachment, AttachmentStatus, Memo, User, Visibility
from app.services import memos as memos_svc
from app.services import notify

logger = structlog.get_logger(__name__)

# A file of the memo that will still change state: the memo is not live while any one remains.
IN_FLIGHT = (AttachmentStatus.uploading, AttachmentStatus.pending, AttachmentStatus.processing)
TITLE_MAX = 80


async def _before_claim() -> None:
    """The instant between a caller's own `ready` commit and its claim. A named no-op, so the race
    test can hold two callers here until both have committed — a race no test could otherwise time.
    """
    return None


async def claim(db: AsyncSession, memo_id: int) -> bool:
    """Stamp the memo announced, if it is live and nobody has. True only for the one caller whose
    UPDATE matched; the caller commits. `updated_at` is set to itself: the column's ORM `onupdate`
    would otherwise move the memo's edit time, and being announced is not an edit."""
    in_flight = exists().where(Attachment.memo_id == Memo.id, Attachment.status.in_(IN_FLIGHT))
    stmt = (
        update(Memo)
        .where(Memo.id == memo_id, Memo.live_notified_at.is_(None), ~in_flight)
        .values(live_notified_at=func.now(), updated_at=Memo.updated_at)
        .returning(Memo.id)
        .execution_options(synchronize_session=False)
    )
    return (await db.execute(stmt)).first() is not None


def first_line(content: str) -> str:
    """The first non-blank line of `content`, trimmed and cut to TITLE_MAX."""
    for line in content.splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1] + "…"
    return ""


async def announce(db: AsyncSession, memo_id: int) -> int:
    """Send the memo-live event to every recipient, reading the memo as it is NOW (its visibility
    at send time). Returns how many users it was addressed to; 0 when the memo is gone."""
    memo = await db.get(Memo, memo_id)
    if memo is None:
        return 0
    guest = await memos_svc.guest_flag(db, memo)
    if guest is not None:
        # A guest batch is owned by the pass issuer; its content is only the trip tag. The title
        # names who really sent it (MEMNG-1164) — the name, never the guest's caption.
        name = guest["contributor_name"]
        title = f"{name} (guest)" if name else "Guest"
    else:
        title = first_line(memo.content)
    event = notify.MemoLive(
        memo_uid=memo.uid,
        owner=memo.owner.display_name or memo.owner.subject,
        title=title,
        attachments=len(memo.attachments),
    )
    recipients = [memo.owner_id]
    if memo.visibility in (Visibility.PROTECTED, Visibility.PUBLIC):
        recipients += list(
            (
                await db.execute(
                    select(User.id)
                    .where(User.notify_on_household_posts.is_(True), User.id != memo.owner_id)
                    .order_by(User.id)
                )
            ).scalars()
        )
    for user_id in recipients:
        await notify.notify(db, user_id, event)
    return len(recipients)


async def announce_if_live(session_factory: async_sessionmaker[AsyncSession], memo_id: int) -> bool:
    """Claim the memo (its own committed transaction), then announce it in a second one. True when
    this call was the one that announced. Errors propagate; `announce_safely` is the best-effort
    wrapper."""
    async with session_factory() as db:
        await _before_claim()
        won = await claim(db, memo_id)
        await db.commit()
    if not won:
        return False
    async with session_factory() as db:
        await announce(db, memo_id)
        await db.commit()  # send_to_user may have pruned a dead endpoint
    return True


async def announce_safely(session_factory: async_sessionmaker[AsyncSession], memo_id: int) -> None:
    """`announce_if_live` for a caller whose own work is already committed (the create route's
    after-response task): a push failure is logged, never raised."""
    try:
        await announce_if_live(session_factory, memo_id)
    except Exception as exc:  # best-effort: the memo is stored whatever happens here
        logger.warning("memo_live_notify_failed", memo_id=memo_id, error=str(exc))
