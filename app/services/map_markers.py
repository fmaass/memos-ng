"""Which located attachments a member may see on the map, and when each was taken.

Visibility here is the FEED's rule, not a rule of its own: `visible_clause` plus "not archived"
plus "ready". A pin is a photo, so a photo the feed hides is a photo the map must hide — deriving
a second rule would let the two drift, and a drift in this direction hands another member's
private photo its location.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, AttachmentStatus, Memo, User
from app.services.memos import visible_clause


def taken_at_expr():
    """When the photo was taken, as an EXPRESSION — no stored date is ever rewritten to fill it.

    `capture_instant` and `captured_at` are both nullable and the archive importer creates rows
    with neither, so the chain has to end in a NOT NULL column for a marker to be placeable on a
    timeline at all.
    """
    return func.coalesce(Attachment.capture_instant, Attachment.captured_at, Attachment.created_at)


async def located_markers(
    db: AsyncSession, user: User
) -> Sequence[tuple[Attachment, str, datetime]]:
    """Every located attachment `user` may see, oldest first, with its memo's uid and taken_at."""
    taken = taken_at_expr()
    stmt = (
        select(Attachment, Memo.uid, taken)
        .join(Memo, Attachment.memo_id == Memo.id)
        .where(
            visible_clause(user),
            Memo.archived_at.is_(None),
            Attachment.status == AttachmentStatus.ready,
            Attachment.latitude.is_not(None),
        )
        # `id` breaks ties so a page of same-second pins keeps one order across requests.
        .order_by(taken.asc(), Attachment.id.asc())
    )
    return [(att, memo_uid, when) for att, memo_uid, when in (await db.execute(stmt)).all()]
