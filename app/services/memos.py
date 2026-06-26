import base64
import re
import secrets
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Attachment,
    AttachmentRendition,
    Memo,
    Reaction,
    Tag,
    User,
    Visibility,
    memo_tags,
)
from app.services import storage

logger = structlog.get_logger(__name__)

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
    created_at: datetime | None = None,
    moved_out: list[str] | None = None,
) -> Memo:
    memo = Memo(
        uid=gen_uid(), owner_id=owner.id, content=content, visibility=visibility, pinned=pinned
    )
    if created_at is not None:
        memo.created_at = created_at  # custom display date (parity with old memos)
    memo.tags = await _get_or_create_tags(db, parse_tags(content))
    db.add(memo)
    await db.flush()
    # created_at is server_default=now() with no Python default → unloaded after flush; placement
    # in link_attachments reads it to build the per-memo folder, so populate it first (an async lazy
    # access on an expired column would raise). A caller-supplied created_at is already loaded; the
    # refresh just reloads the identical persisted value.
    await db.refresh(memo, ["created_at"])
    await link_attachments(db, owner.id, memo, attachment_uids or [], moved_out=moved_out)
    await db.refresh(memo)
    return memo


async def _place_attachment_masters(
    db: AsyncSession, att: Attachment, dest_dir: Path, data_root: Path, moved_out: list[str] | None
) -> None:
    """Move this attachment's still-staged masters (the screen original + each hidden rendition)
    into the memo folder under their real names. ONLY a master in _unfiled/ is moved — an existing
    (committed) master is left in place (new-writes-only). Each moved path is tracked in `moved_out`
    so a rolled-back post unlinks it."""
    if att.storage_path and storage.is_unfiled(att.storage_path, data_root):
        new = await storage.place_master(Path(att.storage_path), dest_dir, att.filename)
        att.storage_path = str(new)
        if moved_out is not None:
            moved_out.append(str(new))
    # Query renditions explicitly rather than via the lazy relationship — async lazy access on a
    # just-added child is fragile, and they were inserted in this same transaction.
    rends = (
        (
            await db.execute(
                select(AttachmentRendition).where(AttachmentRendition.attachment_id == att.id)
            )
        )
        .scalars()
        .all()
    )
    for r in rends:
        if storage.is_unfiled(r.storage_path, data_root):
            new = await storage.place_master(Path(r.storage_path), dest_dir, r.filename)
            r.storage_path = str(new)
            if moved_out is not None:
                moved_out.append(str(new))


async def link_attachments(
    db: AsyncSession,
    owner_id: int,
    memo: Memo,
    uids: Sequence[str],
    moved_out: list[str] | None = None,
) -> None:
    """Link the caller's OWN attachments to a memo. A uid belonging to another user is silently
    ignored so a memo can never claim someone else's media. Used on create and on edit. As each
    attachment is linked, its still-staged masters are placed into the memo's per-memo folder."""
    if not uids:
        return
    owned = (
        (
            await db.execute(
                # FOR UPDATE (ordered by id for a stable lock order) so the memo_id steal-guard
                # below re-checks under a row lock: a concurrent post racing for the same unlinked
                # attachment blocks, then sees it already linked and skips it.
                select(Attachment)
                .where(Attachment.uid.in_(uids), Attachment.owner_id == owner_id)
                .order_by(Attachment.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    data_root = Path(settings.DATA_ROOT)
    dest_dir = storage.memo_dir(data_root, memo.created_at, memo.uid)
    for att in owned:
        # Never STEAL an attachment already linked to a different memo — that would strand the
        # original memo (the defence for the back-compat `attachment_uids` path; the dedup path is
        # also guarded at the source in uploads.finalize). Re-linking to the same memo is a no-op.
        if att.memo_id is not None and att.memo_id != memo.id:
            continue
        att.memo_id = memo.id
        await _place_attachment_masters(db, att, dest_dir, data_root, moved_out)
    await db.flush()


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
    archived_only: bool = False,
    sort: str = "-created_at",
    cursor: str | None = None,
    limit: int = 20,
    with_total: bool = True,
) -> tuple[list[Memo], str | None, int | None]:
    conds = [visible_clause(user)]
    if archived_only:
        conds.append(Memo.archived_at.is_not(None))
    elif not include_archived:
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
    # The COUNT scans every visible memo, so it's the single most expensive part of a feed page —
    # and the feed UI never shows a total. Skip it unless a caller explicitly asks (with_total).
    t_count0 = time.perf_counter()
    total: int | None = None
    if with_total:
        total = (await db.execute(select(func.count()).select_from(Memo).where(where))).scalar_one()
    count_ms = round((time.perf_counter() - t_count0) * 1000, 1)

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
    # This execute also fires the lazy="selectin" relationship loads (tags/owner/attachments/
    # reactions), so select_ms captures the per-page query fan-out, not just the main SELECT.
    t_select0 = time.perf_counter()
    rows = list((await db.execute(stmt)).scalars().all())
    select_ms = round((time.perf_counter() - t_select0) * 1000, 1)

    logger.debug(
        "list_memos.timing",
        count_ms=count_ms,
        select_ms=select_ms,
        rows=len(rows),
        with_total=with_total,
    )
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor, total


async def update_memo(db: AsyncSession, memo: Memo, **fields) -> Memo:
    if "content" in fields and fields["content"] is not None:
        memo.content = fields["content"]
        memo.tags = await _get_or_create_tags(db, parse_tags(memo.content))
    for key in ("visibility", "pinned", "created_at"):
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


async def add_reaction(db: AsyncSession, memo: Memo, user: User, emoji: str) -> None:
    """Idempotent: a user reacting twice with the same emoji is a no-op (unique constraint)."""
    existing = (
        await db.execute(
            select(Reaction).where(
                Reaction.memo_id == memo.id,
                Reaction.user_id == user.id,
                Reaction.emoji == emoji,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(Reaction(memo_id=memo.id, user_id=user.id, emoji=emoji))
        await db.flush()


async def remove_reaction(db: AsyncSession, memo: Memo, user: User, emoji: str) -> None:
    reaction = (
        await db.execute(
            select(Reaction).where(
                Reaction.memo_id == memo.id,
                Reaction.user_id == user.id,
                Reaction.emoji == emoji,
            )
        )
    ).scalar_one_or_none()
    if reaction is not None:
        await db.delete(reaction)
        await db.flush()
