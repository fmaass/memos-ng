import base64
import re
import secrets
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Memo, Tag, User, Visibility, memo_tags

TAG_RE = re.compile(r"#(\w+)")


def parse_tags(content: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in TAG_RE.findall(content or ""):
        seen.setdefault(m, None)
    return list(seen)


def gen_uid() -> str:
    return secrets.token_urlsafe(12)


def encode_cursor(memo: Memo) -> str:
    raw = f"{memo.created_at.isoformat()}|{memo.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts, mid = raw.rsplit("|", 1)
    return datetime.fromisoformat(ts), int(mid)


def visible_clause(user: User):
    """A memo is visible to `user` if they own it OR it is not PRIVATE."""
    return or_(Memo.owner_id == user.id, Memo.visibility != Visibility.PRIVATE)


async def _get_or_create_tags(db: AsyncSession, names: Sequence[str]) -> list[Tag]:
    out = []
    for name in names:
        tag = (await db.execute(select(Tag).where(Tag.name == name))).scalar_one_or_none()
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            await db.flush()
        out.append(tag)
    return out


async def create_memo(
    db: AsyncSession,
    owner: User,
    *,
    content: str,
    visibility: Visibility,
    pinned: bool = False,
    attachment_uids: Sequence[str] | None = None,
) -> Memo:
    memo = Memo(
        uid=gen_uid(), owner_id=owner.id, content=content, visibility=visibility, pinned=pinned
    )
    memo.tags = await _get_or_create_tags(db, parse_tags(content))
    db.add(memo)
    await db.flush()
    if attachment_uids:
        # Link only the caller's OWN attachments — a uid belonging to another user is
        # silently ignored so a memo can't claim someone else's media.
        owned = (
            (
                await db.execute(
                    select(Attachment).where(
                        Attachment.uid.in_(attachment_uids),
                        Attachment.owner_id == owner.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for att in owned:
            att.memo_id = memo.id
        await db.flush()
    await db.refresh(memo)
    return memo


async def get_memo(db: AsyncSession, user: User, uid: str) -> Memo | None:
    memo = (await db.execute(select(Memo).where(Memo.uid == uid))).scalar_one_or_none()
    if memo is None:
        return None
    if memo.owner_id != user.id and memo.visibility == Visibility.PRIVATE:
        return None  # PRIVATE memos are owner-only
    return memo


async def list_memos(
    db: AsyncSession,
    user: User,
    *,
    created_before: datetime | None = None,
    created_after: datetime | None = None,
    tags: Sequence[str] | None = None,
    visibility: Visibility | None = None,
    owner: str | None = None,
    q: str | None = None,
    include_archived: bool = False,
    sort: str = "-created_at",
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[Memo], str | None, int]:
    conds = [visible_clause(user)]
    if not include_archived:
        conds.append(Memo.archived_at.is_(None))
    if created_before:
        conds.append(Memo.created_at < created_before)
    if created_after:
        conds.append(Memo.created_at > created_after)
    if visibility:
        conds.append(Memo.visibility == visibility)
    if owner:
        conds.append(Memo.owner_id.in_(select(User.id).where(User.subject == owner)))
    if q:
        conds.append(
            func.to_tsvector("simple", Memo.content).op("@@")(func.plainto_tsquery("simple", q))
        )
    for tag_name in tags or []:
        conds.append(
            exists().where(
                and_(
                    memo_tags.c.memo_id == Memo.id,
                    memo_tags.c.tag_id == Tag.id,
                    Tag.name == tag_name,
                )
            )
        )

    where = and_(*conds)
    total = (await db.execute(select(func.count()).select_from(Memo).where(where))).scalar_one()

    descending = not sort.lstrip("-") == sort  # "-created_at" → desc
    if cursor:
        cdt, cid = decode_cursor(cursor)
        if descending:
            conds.append(or_(Memo.created_at < cdt, and_(Memo.created_at == cdt, Memo.id < cid)))
        else:
            conds.append(or_(Memo.created_at > cdt, and_(Memo.created_at == cdt, Memo.id > cid)))

    order = (
        (Memo.created_at.desc(), Memo.id.desc())
        if descending
        else (Memo.created_at.asc(), Memo.id.asc())
    )
    stmt = select(Memo).where(and_(*conds)).order_by(*order).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor, total


async def update_memo(db: AsyncSession, memo: Memo, **fields) -> Memo:
    if "content" in fields and fields["content"] is not None:
        memo.content = fields["content"]
        memo.tags = await _get_or_create_tags(db, parse_tags(memo.content))
    for key in ("visibility", "pinned"):
        if fields.get(key) is not None:
            setattr(memo, key, fields[key])
    if fields.get("archived") is True and memo.archived_at is None:
        memo.archived_at = func.now()
    if fields.get("archived") is False:
        memo.archived_at = None
    await db.flush()
    await db.refresh(memo)
    return memo


async def delete_memo(db: AsyncSession, memo: Memo) -> None:
    await db.delete(memo)
    await db.flush()


async def list_tags(db: AsyncSession, user: User) -> list[tuple[str, int]]:
    stmt = (
        select(Tag.name, func.count(memo_tags.c.memo_id))
        .select_from(Tag)
        .join(memo_tags, memo_tags.c.tag_id == Tag.id)
        .join(Memo, Memo.id == memo_tags.c.memo_id)
        .where(visible_clause(user), Memo.archived_at.is_(None))
        .group_by(Tag.name)
        .order_by(func.count(memo_tags.c.memo_id).desc())
    )
    return [(name, count) for name, count in (await db.execute(stmt)).all()]


async def heatmap(db: AsyncSession, user: User) -> dict[str, int]:
    day = cast(func.date(Memo.created_at), String)
    stmt = (
        select(day, func.count())
        .where(visible_clause(user), Memo.archived_at.is_(None))
        .group_by(day)
    )
    return {d: c for d, c in (await db.execute(stmt)).all()}
