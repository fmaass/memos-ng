"""Collections (trips): the household-shared record and its exclusions (MEMNG-746).

A collection is edited by any signed-in member and the last write wins (owner decision 2026-10-10),
so nothing here checks who is asking — except `member_count`, whose answer is the caller's view of
the members, through the library's own visibility. Which attachments are members is
`app.services.attachments.collection_member_clause`; this module never restates it.

Every write that two clients can race is settled by the database, not by a read-then-write: a keyed
create and an exclusion are each one row however many identical requests overlap (both JOIN — the
loser gets the winner's row), and the record's shape is validated in full before any statement so a
bad record is a `CollectionInvalid`, never an IntegrityError from the CHECKs that back it.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Collection, CollectionExclusion, Memo, User
from app.services.attachments import collection_member_clause, library_filters
from app.services.memos import gen_uid

# What `update_collection` may change. `uid`, `created_by`, the key and the timestamps are the
# record's identity and history, not its content.
EDITABLE_FIELDS = frozenset({"title", "tag", "span_start", "span_end", "tz", "cover_attachment_id"})

# A keyed create that loses the race reads the winner back. The winner can only be gone if it was
# deleted between the conflict and the read, so a second pass inserts afresh; a third would mean
# the key is being created and deleted continuously, which is not a state worth looping on.
_CREATE_ATTEMPTS = 3


class CollectionInvalid(ValueError):
    """The record would break the collection's shape: no tag and no span, half a span, a reversed
    span, a bound without an offset, an empty tag, or a `tz` that is no IANA zone. The router
    answers it with 422; the database's CHECKs stay behind it as the last line."""


class CollectionGone(LookupError):
    """The collection was deleted between the caller loading it and this write; the router
    answers it as it answers an unknown uid."""


class CoverGone(LookupError):
    """The attachment named as the cover does not exist — never did, or was deleted before this
    write could hold it. The router answers it as it answers an unknown uid."""


async def _hold_cover(db: AsyncSession, cover_attachment_id: int | None) -> None:
    """Take FOR KEY SHARE on the cover attachment's row BEFORE any collection row (lock order
    A < C, docs/LOCK-ORDER.md). Held, the row cannot be deleted under this write, so the FK check
    at flush has nothing to wait on; a delete that already holds the row makes this wait here, while
    this transaction holds no collection row, and then find it gone."""
    if cover_attachment_id is None:
        return
    held = await db.execute(
        select(Attachment.id)
        .where(Attachment.id == cover_attachment_id)
        .with_for_update(read=True, key_share=True)
    )
    if held.scalar_one_or_none() is None:
        raise CoverGone(cover_attachment_id)


def _validate(
    *,
    tag: str | None,
    span_start: datetime | None,
    span_end: datetime | None,
    tz: str | None,
) -> None:
    if tag is not None and not tag.strip():
        raise CollectionInvalid("tag is empty")
    for bound in (span_start, span_end):
        if bound is not None and bound.utcoffset() is None:
            raise CollectionInvalid("a span bound must carry a UTC offset")
    if (span_start is None) != (span_end is None):
        raise CollectionInvalid("a span needs both bounds")
    if tag is None and span_start is None:
        raise CollectionInvalid("a collection needs a tag, a span, or both")
    if span_start is not None and span_end is not None and span_end < span_start:
        raise CollectionInvalid("the span ends before it starts")
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CollectionInvalid("tz is not an IANA zone name") from exc


async def get_by_idempotency_key(db: AsyncSession, created_by: int, key: str) -> Collection | None:
    """The collection this member already created under `key`, or None. Scoped to the creator for
    the memo route's reason: the key is the CLIENT's name for one create, not a global one."""
    stmt = select(Collection).where(
        Collection.created_by == created_by, Collection.idempotency_key == key
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_collection(
    db: AsyncSession,
    user: User,
    *,
    title: str,
    tag: str | None = None,
    span_start: datetime | None = None,
    span_end: datetime | None = None,
    tz: str | None = None,
    cover_attachment_id: int | None = None,
    idempotency_key: str | None = None,
) -> tuple[Collection, bool]:
    """Store one collection; returns it and whether THIS call created it.

    With a key, a replay — sequential or racing — returns the record the first create stored and
    `False`, whatever the replay's own fields say: the partial unique index on (creator, key) makes
    the insert a no-op for every caller but one, and the losers read the winner back. Without a
    key, every call is a new record.

    The replay lookup comes FIRST, before validation and before the cover is held (the memo
    route's order): a retry whose response was lost may name a cover that has since been
    reclaimed, and the answer to it is still the record the first call stored, not CoverGone."""
    if idempotency_key is not None:
        existing = await get_by_idempotency_key(db, user.id, idempotency_key)
        if existing is not None:
            return existing, False
    _validate(tag=tag, span_start=span_start, span_end=span_end, tz=tz)
    try:
        await _hold_cover(db, cover_attachment_id)
    except CoverGone:
        # Between the missed lookup and here, a same-key create may have won and its cover been
        # reclaimed since: the winner is still this retry's answer, so ask once more.
        if idempotency_key is not None:
            existing = await get_by_idempotency_key(db, user.id, idempotency_key)
            if existing is not None:
                return existing, False
        raise
    values = {
        "uid": gen_uid(),
        "title": title,
        "tag": tag,
        "span_start": span_start,
        "span_end": span_end,
        "tz": tz,
        "cover_attachment_id": cover_attachment_id,
        "created_by": user.id,
        "idempotency_key": idempotency_key,
    }
    if idempotency_key is None:
        new_id = (
            await db.execute(pg_insert(Collection).values(values).returning(Collection.id))
        ).scalar_one()
        return await _load(db, new_id), True
    for _ in range(_CREATE_ATTEMPTS):
        stmt = (
            pg_insert(Collection)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[Collection.created_by, Collection.idempotency_key],
                index_where=Collection.idempotency_key.is_not(None),
            )
            .returning(Collection.id)
        )
        new_id = (await db.execute(stmt)).scalar_one_or_none()
        if new_id is not None:
            return await _load(db, new_id), True
        existing = await get_by_idempotency_key(db, user.id, idempotency_key)
        if existing is not None:
            return existing, False
    raise RuntimeError("a keyed collection create kept losing to a record that then vanished")


async def _load(db: AsyncSession, collection_id: int) -> Collection:
    stmt = select(Collection).where(Collection.id == collection_id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def get_collection(db: AsyncSession, uid: str) -> Collection | None:
    return (await db.execute(select(Collection).where(Collection.uid == uid))).scalar_one_or_none()


async def list_collections(db: AsyncSession) -> list[Collection]:
    """Every collection (a household object), the latest span first and span-less ones last."""
    stmt = select(Collection).order_by(
        Collection.span_start.desc().nulls_last(),
        Collection.created_at.desc(),
        Collection.id.desc(),
    )
    return list((await db.execute(stmt)).scalars().all())


async def update_collection(
    db: AsyncSession, collection: Collection, changes: Mapping[str, object]
) -> Collection:
    """Apply `changes`: a field that is absent stays as it is, and an explicit None CLEARS it (the
    memo service's "None means unchanged" would make a cover or a tag impossible to remove).

    The RESULTING record is validated before anything is written, so a change that would leave half
    a span or no source at all raises `CollectionInvalid` and leaves the record exactly as it was.
    "Resulting" means this change applied to the row AS IT IS NOW, not as the caller loaded it: the
    row is locked (FOR UPDATE) and re-read first, so of two overlapping edits — one clearing the tag
    while the span remains, one clearing the span while the tag remains — the second waits for the
    first, sees no tag, and is refused here rather than by the source CHECK as an IntegrityError.
    The re-read overwrites `collection` with the current row, which is last-write-wins for every
    field this change does not name. A field outside `EDITABLE_FIELDS` is a programming error of
    the caller and raises KeyError; a row deleted meanwhile raises `CollectionGone`, and a cover
    that does not exist `CoverGone` — its row is held FOR KEY SHARE before the collection row, the
    one order every path takes (A < C)."""
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise KeyError(f"not editable: {sorted(unknown)}")
    collection_id = collection.id
    await _hold_cover(db, cast(int | None, changes.get("cover_attachment_id")))
    locked = (
        select(Collection)
        .where(Collection.id == collection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (await db.execute(locked)).scalar_one_or_none() is None:
        raise CollectionGone(collection_id)

    def after(name: str) -> object:
        return changes[name] if name in changes else getattr(collection, name)

    _validate(
        tag=cast(str | None, after("tag")),
        span_start=cast(datetime | None, after("span_start")),
        span_end=cast(datetime | None, after("span_end")),
        tz=cast(str | None, after("tz")),
    )
    for name, value in changes.items():
        setattr(collection, name, value)
    await db.flush()
    await db.refresh(collection)
    return collection


async def delete_collection(db: AsyncSession, collection: Collection) -> None:
    """Remove the record; its exclusion rows go with it by the FK's ON DELETE CASCADE."""
    await db.delete(collection)
    await db.flush()


async def exclude(db: AsyncSession, collection: Collection, attachment: Attachment) -> None:
    """Take `attachment` out of `collection`. Idempotent and race-safe: the pair is the primary key
    and a second insert of it does nothing, so overlapping identical requests are one row."""
    stmt = (
        pg_insert(CollectionExclusion)
        .values(collection_id=collection.id, attachment_id=attachment.id)
        .on_conflict_do_nothing(
            index_elements=[CollectionExclusion.collection_id, CollectionExclusion.attachment_id]
        )
    )
    await db.execute(stmt)


async def include(db: AsyncSession, collection: Collection, attachment: Attachment) -> None:
    """Undo `exclude`. Idempotent: deleting a row that is not there deletes nothing."""
    await include_attachments(db, collection.id, [attachment.id])


async def include_attachments(
    db: AsyncSession, collection_id: int, attachment_ids: Sequence[int]
) -> None:
    """Undo `exclude` for several attachments of one collection in ONE statement — the class is
    acquired once per transaction (docs/LOCK-ORDER.md R2). Idempotent like `include`. Takes the
    collection's id rather than the record, because the inclusion route calls it after a memo
    transaction may have rolled back and expired every loaded object."""
    await db.execute(
        delete(CollectionExclusion).where(
            CollectionExclusion.collection_id == collection_id,
            CollectionExclusion.attachment_id.in_(attachment_ids),
        )
    )


async def member_count(db: AsyncSession, collection: Collection, user: User) -> int:
    """How many members `user` can see — the library's filters plus the membership clause over the
    same outer join the library list uses, so the count is the length of the member read."""
    stmt = (
        select(func.count())
        .select_from(Attachment)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        .where(*library_filters(user, None, None), collection_member_clause(collection))
    )
    return (await db.execute(stmt)).scalar_one()
