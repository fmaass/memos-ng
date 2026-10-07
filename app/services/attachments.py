"""The media library's shared predicates and its date expression — ONE definition each.

`taken_at_expr` is read by the library list, its day/month/year buckets and the Places map; the
visibility, kind and tag predicates by every library surface. A second copy of any of them is how
two surfaces come to disagree about which photo exists, or when it was taken — and a visibility
copy that drifts hands another member's private photo to a bucket cover.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, Text, and_, exists, func, select
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Memo, Tag, User, memo_tags
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


def library_filters(user: User, kind: str | None, tag: str | None) -> list[ColumnElement[bool]]:
    conds = [library_visible(user)]
    by_kind = kind_clause(kind)
    if by_kind is not None:
        conds.append(by_kind)
    if tag is not None:
        conds.append(tagged_clause(tag))
    return conds


def bucket_key(start: datetime, size: BucketSize) -> str:
    """The bucket's local start as `2026-09-04` / `2026-09` / `2026`. Formatted field by field:
    `%Y` does not zero-pad years below 1000 on this runtime (see app/api_types.py)."""
    if size == "year":
        return f"{start.year:04d}"
    if size == "month":
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.year:04d}-{start.month:02d}-{start.day:02d}"


async def library_buckets(
    db: AsyncSession, user: User, size: BucketSize, tz: str, kind: str | None, tag: str | None
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
        .where(*library_filters(user, kind, tag))
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
    db: AsyncSession, user: User, kind: str | None
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
        .where(*library_filters(user, kind, None))
        .group_by(Tag.name)
        .order_by(count.desc(), Tag.name.asc())
    )
    return [(name, n) for name, n in (await db.execute(stmt)).all()]
