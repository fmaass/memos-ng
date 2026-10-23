"""The media library's shared predicates and its date expression — ONE definition each.

`taken_at_expr` is read by the library list, its day/month/year buckets and the Places map; the
visibility, kind and tag predicates by every library surface. A second copy of any of them is how
two surfaces come to disagree about which photo exists, or when it was taken — and a visibility
copy that drifts hands another member's private photo to a bucket cover.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, Text, and_, exists, false, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Attachment,
    Collection,
    CollectionExclusion,
    CollectionMember,
    Memo,
    Tag,
    User,
    Visibility,
    attachment_tags,
    memo_tags,
)
from app.services.memos import visible_clause

BucketSize = Literal["day", "month", "year"]


def taken_at_expr():
    """When the photo was taken, as an EXPRESSION over `attachments` outer-joined to `memos` —
    no stored date is ever rewritten to fill it, and no index or migration backs it.

    The owner's rule (2026-09-22): the EXIF capture instant, else the parent memo's date; the
    attachment's own `created_at` exists only for an orphan with no memo, and is NOT NULL, so the
    chain always yields a value. `captured_at` is deliberately absent: it is `capture_instant or
    client_mtime or now` (app/services/uploads.py), so it would date a photo by the browser's file
    mtime or by the day a migration ran. `capture_civil` is a zone-less wall clock and is not
    converted here — the SPA owns that policy (plan 3929, terminology).
    """
    return func.coalesce(Attachment.capture_instant, Memo.created_at, Attachment.created_at)


def library_visible(user: User) -> ColumnElement[bool]:
    """Own media (orphans included, where the outer-joined Memo is NULL) OR media on a memo the
    user can see. NULL semantics do the rest: another member's orphan has no memo, so
    `visible_clause` is NULL for it and the row is excluded."""
    return (Attachment.owner_id == user.id) | visible_clause(user)


def kind_clause(kind: str | None) -> ColumnElement[bool] | None:
    """`?type=image|video` by mime prefix; anything else filters nothing, as the list always did."""
    if kind == "image":
        return Attachment.mime.like("image/%")
    if kind == "video":
        return Attachment.mime.like("video/%")
    return None


def tagged_clause(tag_name: str) -> ColumnElement[bool]:
    """The attachment's memo carries the tag named exactly `tag_name` — the feed's predicate
    (app/services/memos.py `list_memos`), correlated to the outer-joined Memo, so an orphan never
    matches. It is ADDED under `library_visible`, never a substitute for it."""
    return exists().where(
        and_(
            memo_tags.c.memo_id == Memo.id,
            memo_tags.c.tag_id == Tag.id,
            Tag.name == tag_name,
        )
    )


def picture_tagged_clause(tag_name: str) -> ColumnElement[bool]:
    """The attachment ITSELF carries the picture tag named exactly `tag_name` (MEMNG-863) — never
    its memo's tags, which `tagged_clause` reads, so the two namespaces never admit each other's
    rows. An EXISTS, so a picture is one row however it matches; correlated to `attachments`
    alone, so an orphan matches like any other picture. ADDED under `library_visible`."""
    return exists().where(
        and_(
            attachment_tags.c.attachment_id == Attachment.id,
            attachment_tags.c.tag_id == Tag.id,
            Tag.name == tag_name,
        )
    )


def memo_tag_names_expr() -> ColumnElement[Sequence[str]]:
    """The tag names of the outer-joined Memo, sorted by name — the order `MemoOut.tags` uses.
    A correlated ARRAY(subquery) rides on the row the library SELECT already produces, so a page
    costs no extra statement, and it is `{}` (never NULL) for an untagged memo and an orphan."""
    names = (
        select(Tag.name)
        .join(memo_tags, memo_tags.c.tag_id == Tag.id)
        .where(memo_tags.c.memo_id == Memo.id)
        .order_by(Tag.name.asc())
        .scalar_subquery()
    )
    return func.array(names, type_=ARRAY(Text))


def library_filters(
    user: User,
    kind: str | None,
    tag: str | None,
    *,
    mine: bool = False,
    visibility: Visibility | None = None,
    picture_tag: str | None = None,
) -> list[ColumnElement[bool]]:
    """`library_visible` and whatever NARROWS it — never a substitute for it, so no combination
    of these reaches a row the caller could not already see (MEMNG-785).

    `mine` is ATTACHMENT ownership, the item's own `mine`, so the caller's orphans stay under it
    and another member's picture on the caller's memo does not. `visibility` compares the
    outer-joined memo's, so an orphan (NULL memo) is absent under every value; PRIVATE is accepted
    as the memo listing accepts it, and `library_visible` still bounds it to the caller's own."""
    conds = [library_visible(user)]
    if mine:
        conds.append(Attachment.owner_id == user.id)
    if visibility is not None:
        conds.append(Memo.visibility == visibility)
    by_kind = kind_clause(kind)
    if by_kind is not None:
        conds.append(by_kind)
    if tag is not None:
        conds.append(tagged_clause(tag))
    if picture_tag is not None:
        conds.append(picture_tagged_clause(picture_tag))
    return conds


def collection_member_clause(collection: Collection) -> ColumnElement[bool]:
    """Which attachments belong to `collection` (MEMNG-746): members have a memo, are not detached,
    match the collection's tag OR lie inside its span, and are not excluded.

    "Have a memo" is `memo_id IS NOT NULL` — a trip is the memos with that tag or in that range, so
    an orphan never belongs. "Not detached" is `detached_at IS NULL`, which also catches a row
    stamped without its link being cleared; detachments older than that column carry no stamp and
    are caught by the memo part alone. The tag arm is the library's own `tagged_clause`; the span
    arm reads the library's own date, `taken_at_expr()`, between both bounds INCLUSIVE — so
    membership and the grid's ordering agree, and a photo without a capture instant is dated by its
    memo. Each arm is present only when the collection has it.

    Visibility stays in `library_filters`, never here: a caller appends this clause to that list
    over `attachments` OUTER JOINED to `memos`, exactly as `GET /api/attachments` builds its query,
    so the correlated `Memo` columns resolve against that join and nothing a member sees can differ
    from what the library shows the same caller.

    An ALBUM (MEMNG-802) has no tag and no span: its one arm is its own `collection_members` row,
    and exclusions do not apply to it — a picture leaves an album by losing that row. The memo and
    detachment terms stay OUTSIDE the kind switch, so a detached album member leaves the listing
    exactly as a trip's does.
    """
    if collection.kind == "album":
        held = exists().where(
            CollectionMember.collection_id == collection.id,
            CollectionMember.attachment_id == Attachment.id,
        )
        return and_(Attachment.memo_id.is_not(None), Attachment.detached_at.is_(None), held)
    arms: list[ColumnElement[bool]] = []
    if collection.tag is not None:
        arms.append(tagged_clause(collection.tag))
    if collection.span_start is not None and collection.span_end is not None:
        arms.append(taken_at_expr().between(collection.span_start, collection.span_end))
    excluded = exists().where(
        CollectionExclusion.collection_id == collection.id,
        CollectionExclusion.attachment_id == Attachment.id,
    )
    return and_(
        Attachment.memo_id.is_not(None),
        Attachment.detached_at.is_(None),
        or_(*arms) if arms else false(),
        ~excluded,
    )


LibraryOrder = Literal["desc", "asc", "taken_desc", "taken_asc"]
TAKEN_ORDERS = ("taken_desc", "taken_asc")


def library_page_statement(
    filters: Sequence[ColumnElement[bool]],
    order: LibraryOrder,
    *,
    after_id: int | None = None,
    after_taken: tuple[datetime, int] | None = None,
):
    """The keyset read of the media library, ordered and resumed after a decoded cursor: rows of
    `(Attachment, Memo.uid, takenAt, tag names, Memo.visibility)` — the last NULL for an orphan —
    over `attachments` OUTER JOINED to `memos`, so every predicate in `filters` resolves against
    the same join `library_filters` is written for.

    ONE definition for every surface that pages the library — `GET /api/attachments` and a
    collection's member read — so cursors, ordering and the item's columns cannot drift between
    them. `after_taken` resumes a taken order, `after_id` an upload order; the caller decodes the
    cursor and refuses the wrong kind, and adds its own LIMIT."""
    taken = taken_at_expr()
    stmt = (
        # `Memo.uid` and the taken date ride along on the row the join already produces: the memo
        # is many-to-one from the attachment, so selecting them multiplies no rows, and it costs
        # no statement — a relationship + per-item read would be one SELECT per tile (pinned by a
        # statement-count test in tests/test_assets.py).
        select(Attachment, Memo.uid, taken, memo_tag_names_expr(), Memo.visibility)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        .where(*filters)
    )
    if order in TAKEN_ORDERS:
        # A composite keyset: taken_at alone is not unique (a burst, a memo date shared by every
        # migrated item), so `id` breaks the tie in the same direction and the pair is total.
        # The chain ends in NOT NULL `created_at`, so no row escapes the comparison as NULL.
        ascending = order == "taken_asc"
        if after_taken is not None:
            c_taken, c_id = after_taken
            if ascending:
                after = or_(taken > c_taken, and_(taken == c_taken, Attachment.id > c_id))
            else:
                after = or_(taken < c_taken, and_(taken == c_taken, Attachment.id < c_id))
            stmt = stmt.where(after)
        return stmt.order_by(
            *(
                (taken.asc(), Attachment.id.asc())
                if ascending
                else (taken.desc(), Attachment.id.desc())
            )
        )
    if after_id is not None:
        # STRICT comparison, exactly as the memo feed's cursor compares: `<=`/`>=` would serve
        # the boundary row a second time. The filters are re-applied per page, so a page is never
        # cut before the filter runs. The direction follows `order`, and the cursor itself stays
        # what it always was — an opaque id, carrying NO direction, so an already-issued cursor
        # keeps working. Replaying one under the other order does not fail: it simply continues
        # from that id the other way. Keeping order consistent across a walk is the client's job,
        # exactly as it already is for `type` and `limit`, which the cursor does not carry either.
        stmt = stmt.where(Attachment.id > after_id if order == "asc" else Attachment.id < after_id)
    # Ordering is `id` alone in either direction, and that order is already TOTAL: id is the
    # primary key, so no two rows can tie and a keyset walk can neither skip nor repeat — a
    # created_at tie (bulk import, same-second upload batch) is broken by construction. It is
    # also the one order this table is indexed for (attachments carries no created_at index),
    # and it is the order the library already displayed; (created_at DESC, id DESC) would have
    # reshuffled it. Probed against production 2026-08-26 and re-probed 2026-08-27: no row
    # disagreed between id order and created_at order, so "oldest/newest first" means the same
    # thing either way and `order=asc` needs no created_at ordering, composite cursor, index or
    # migration — it is this same keyset read backwards. The table held 1795 attachments on
    # 2026-09-22 (MEMNG-501); the order probe was not re-run at that count.
    return stmt.order_by(Attachment.id.asc() if order == "asc" else Attachment.id.desc())


def bucket_key(start: datetime, size: BucketSize) -> str:
    """The bucket's local start as `2026-09-04` / `2026-09` / `2026`. Formatted field by field:
    `%Y` does not zero-pad years below 1000 on this runtime (see app/api_types.py)."""
    if size == "year":
        return f"{start.year:04d}"
    if size == "month":
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.year:04d}-{start.month:02d}-{start.day:02d}"


# ASCII digits only, matched whole: `\d` takes any Unicode digit and `$` a trailing newline.
_BUCKET_KEY = re.compile(r"([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?")


def parse_bucket_key(key: str) -> tuple[BucketSize, datetime] | None:
    """The inverse of `bucket_key`: the size a key names and its local start, or None for a key
    `bucket_key` could not have written (a malformed one, or a date the calendar lacks)."""
    m = _BUCKET_KEY.fullmatch(key)
    if m is None:
        return None
    year, month, day = m.groups()
    try:
        start = datetime(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return None
    return ("day" if day else "month" if month else "year"), start


def bucket_bound_clause(
    size: BucketSize, start: datetime, tz: str, ascending: bool
) -> ColumnElement[bool]:
    """The rows a taken-order walk opened at a bucket begins with: the bucket and everything
    before it in the walk's direction. It is the buckets' own expression compared with the
    bucket's local start, so the first row is exactly where `library_buckets` counted it, and no
    end of the bucket is ever computed (the last year has no next one)."""
    local = func.date_trunc(size, func.timezone(tz, taken_at_expr()))
    return local >= start if ascending else local <= start


async def library_buckets(
    db: AsyncSession,
    user: User,
    size: BucketSize,
    tz: str,
    kind: str | None,
    tag: str | None,
    *,
    mine: bool = False,
    visibility: Visibility | None = None,
    picture_tag: str | None = None,
) -> list[tuple[str, int, str, str, list[str]]]:
    """(key, count, cover uid, cover mime, tags) per calendar bucket of `tz`, newest first. The
    cover is the bucket's newest row by (taken_at, id) among the rows the filters ADMITTED — it is
    picked in the same statement, after the visibility predicate, so no excluded row can ever be a
    cover. Its mime rides along from the same ranked row: a video cover is shown by its poster.

    The tags are those of the memos the ADMITTED rows hang on, each once, the tag on the most of
    the bucket's pictures first, then by name — every one of them; a client that shows three
    counts the rest. They are aggregated from the same filtered rows in the same statement, so a
    bucket's count and its tags are read at one moment and no hidden picture lends a tag.

    `AT TIME ZONE` converts before `date_trunc`: every link of the chain is `timestamptz`, so an
    item at 22:30Z on 31 August is 00:30 on 1 September in Zurich and lands there. The bucket is
    computed once in an inner select and partitioned by that COLUMN, so the size and zone are
    each bound exactly once."""
    taken = taken_at_expr()
    rows = (
        select(
            Attachment.id.label("id"),
            Attachment.uid.label("uid"),
            Attachment.mime.label("mime"),
            taken.label("taken"),
            Attachment.memo_id.label("memo_id"),
            func.date_trunc(size, func.timezone(tz, taken)).label("bucket"),
        )
        .select_from(Attachment)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        .where(
            *library_filters(
                user, kind, tag, mine=mine, visibility=visibility, picture_tag=picture_tag
            )
        )
        .cte("library_rows")
    )
    ranked = select(
        rows.c.bucket,
        rows.c.uid,
        rows.c.mime,
        func.count().over(partition_by=rows.c.bucket).label("n"),
        func.row_number()
        .over(partition_by=rows.c.bucket, order_by=(rows.c.taken.desc(), rows.c.id.desc()))
        .label("rank"),
    ).subquery()
    pictures = func.count(rows.c.id).label("pictures")
    per_tag = (
        select(rows.c.bucket, Tag.name.label("name"), pictures)
        .select_from(rows)
        .join(memo_tags, memo_tags.c.memo_id == rows.c.memo_id)
        .join(Tag, Tag.id == memo_tags.c.tag_id)
        .group_by(rows.c.bucket, Tag.name)
        .subquery()
    )
    tag_lists = (
        select(
            per_tag.c.bucket,
            func.array_agg(
                aggregate_order_by(per_tag.c.name, per_tag.c.pictures.desc(), per_tag.c.name.asc())
            ).label("tags"),
        )
        .group_by(per_tag.c.bucket)
        .subquery()
    )
    stmt = (
        select(ranked.c.bucket, ranked.c.n, ranked.c.uid, ranked.c.mime, tag_lists.c.tags)
        .outerjoin(tag_lists, tag_lists.c.bucket == ranked.c.bucket)
        .where(ranked.c.rank == 1)
        .order_by(ranked.c.bucket.desc())
    )
    return [
        (bucket_key(b, size), n, uid, mime, list(tags or ()))
        for b, n, uid, mime, tags in (await db.execute(stmt)).all()
    ]


async def library_tag_counts(
    db: AsyncSession,
    user: User,
    kind: str | None,
    *,
    mine: bool = False,
    visibility: Visibility | None = None,
) -> list[tuple[str, int]]:
    """Each tag with the number of library items (not memos) under it that `user` may see, under
    the same kind filter the grid applies — so a count equals the grid's length under that tag.
    A tag whose memos carry no media is absent. Most items first, then by name."""
    count = func.count(Attachment.id)
    stmt = (
        select(Tag.name, count)
        .select_from(Attachment)
        .join(Memo, Memo.id == Attachment.memo_id)
        .join(memo_tags, memo_tags.c.memo_id == Memo.id)
        .join(Tag, Tag.id == memo_tags.c.tag_id)
        .where(*library_filters(user, kind, None, mine=mine, visibility=visibility))
        .group_by(Tag.name)
        .order_by(count.desc(), Tag.name.asc())
    )
    return [(name, n) for name, n in (await db.execute(stmt)).all()]


async def library_picture_tag_counts(
    db: AsyncSession,
    user: User,
    kind: str | None,
    *,
    mine: bool = False,
    visibility: Visibility | None = None,
) -> list[tuple[str, int]]:
    """Each PICTURE tag (MEMNG-863) with the number of library items carrying it that `user` may
    see — memo tags never count here. The population is the listing's own: `attachments` OUTER
    joined to `memos` under the same `library_filters`, so an owner's tagged orphan counts and no
    readiness condition applies, and a tag's count is the length of `?pictureTag=` under the same
    `type`, `mine` and `visibility`. The pair is the primary key, so a picture counts once per tag.
    A tag on no visible picture is absent. Most items first, then by name."""
    count = func.count(Attachment.id)
    stmt = (
        select(Tag.name, count)
        .select_from(Attachment)
        .outerjoin(Memo, Memo.id == Attachment.memo_id)
        .join(attachment_tags, attachment_tags.c.attachment_id == Attachment.id)
        .join(Tag, Tag.id == attachment_tags.c.tag_id)
        .where(*library_filters(user, kind, None, mine=mine, visibility=visibility))
        .group_by(Tag.name)
        .order_by(count.desc(), Tag.name.asc())
    )
    return [(name, n) for name, n in (await db.execute(stmt)).all()]
