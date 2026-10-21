"""The chosen covers of derived collections — tags, months and years (MEMNG-853).

A cover is household-wide: one row per `(kind, key)`, and the last write wins. Who may choose a
picture is the router's question (`_viewable`); this module only stores and reads, and reads only
what the READER may see, so a cover on a picture the reader cannot see is absent rather than an
error, and the client shows its default.

Lock order (docs/LOCK-ORDER.md): a write takes FOR KEY SHARE on the attachment row FIRST
(`collections._hold_cover`, A) and only then writes its one cover row (C), the same A < C every
attachment delete follows when its CASCADE reaches the cover rows.
"""

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, CollectionCover, Memo, User
from app.services.attachments import library_visible
from app.services.collections import CoverGone, _hold_cover

__all__ = ["CoverGone", "clear_cover", "list_covers", "set_cover"]


async def set_cover(db: AsyncSession, kind: str, key: str, attachment: Attachment) -> None:
    """Make `attachment` the cover of `(kind, key)`, replacing any earlier choice in ONE upsert,
    so two first writes of one key are one row, never an IntegrityError. `CoverGone` when the
    attachment was deleted after the caller looked it up."""
    await _hold_cover(db, attachment.id)
    stmt = insert(CollectionCover).values(kind=kind, key=key, attachment_id=attachment.id)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[CollectionCover.kind, CollectionCover.key],
            set_={"attachment_id": stmt.excluded.attachment_id},
        )
    )


async def clear_cover(db: AsyncSession, kind: str, key: str) -> None:
    """Forget the cover of `(kind, key)`; clearing one that is not set changes nothing."""
    await db.execute(
        delete(CollectionCover).where(CollectionCover.kind == kind, CollectionCover.key == key)
    )


async def list_covers(db: AsyncSession, user: User) -> Sequence[tuple[str, str, str]]:
    """Every cover whose attachment `user` may see, as `(kind, key, attachment uid)` ordered by
    kind then key. `library_visible` over the attachment OUTER JOINED to its memo is the same rule
    `_viewable` applies: own media, or media on a memo the user can see."""
    rows = await db.execute(
        select(CollectionCover.kind, CollectionCover.key, Attachment.uid)
        .join(Attachment, Attachment.id == CollectionCover.attachment_id)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        .where(library_visible(user))
        .order_by(CollectionCover.kind, CollectionCover.key)
    )
    return [(kind, key, uid) for kind, key, uid in rows.all()]
