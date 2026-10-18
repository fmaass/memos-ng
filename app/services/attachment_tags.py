"""Picture tags (MEMNG-863): set and clear one tag on one attachment.

A picture tag is the pair (attachment, tag) in `attachment_tags`; the name row is the memo tags'
own `tags` row, created on demand by the same helper the memo write uses. Both operations are
idempotent by construction — the pair is the primary key and a second insert of it does nothing, a
delete of an absent pair deletes nothing — so overlapping identical requests leave one row and
answer alike. Ownership is the CALLER's check (app/routers/assets.py); these take the row it
already resolved. Neither commits: the route does, before it answers.
"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_types import tag_name
from app.models import Attachment, Tag, attachment_tags
from app.services.memos import _get_or_create_tags


async def add(db: AsyncSession, att: Attachment, name: str) -> None:
    """Tag `att` with `name`, creating the tag row if no memo or picture carries it yet."""
    (tag,) = await _get_or_create_tags(db, [tag_name(name)])
    await db.execute(
        pg_insert(attachment_tags)
        .values(attachment_id=att.id, tag_id=tag.id)
        .on_conflict_do_nothing(
            index_elements=[attachment_tags.c.attachment_id, attachment_tags.c.tag_id]
        )
    )


async def remove(db: AsyncSession, att: Attachment, name: str) -> None:
    """Take `name` off `att`. The tag row stays even when nothing carries it any more."""
    await db.execute(
        delete(attachment_tags).where(
            attachment_tags.c.attachment_id == att.id,
            attachment_tags.c.tag_id.in_(select(Tag.id).where(Tag.name == tag_name(name))),
        )
    )
