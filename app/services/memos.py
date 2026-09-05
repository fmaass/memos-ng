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
    MemoEditKey,
    Reaction,
    Tag,
    User,
    Visibility,
    memo_tags,
)
from app.services import storage
from app.services.cursors import InvalidCursor, decode_cursor_payload, encode_cursor_payload

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
    return encode_cursor_payload(f"{memo.created_at.isoformat()}|{memo.id}")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """`InvalidCursor` (→ 422) for anything we did not mint — never an unhandled 500, and never a
    silent restart from page 1, which would make a paging client loop over the first page for
    ever. Both failure modes were live before MEMNG-163: the decode had no guard at all."""
    raw = decode_cursor_payload(cursor)
    ts, sep, mid = raw.rpartition("|")
    if not sep:
        raise InvalidCursor("cursor is not a keyset token")
    try:
        return datetime.fromisoformat(ts), int(mid)
    except ValueError:
        raise InvalidCursor("cursor is not a keyset token") from None


def owner_subjects(owner: str | Sequence[str] | None) -> list[str]:
    """The `User.subject`s an owner filter names, as an OR-set — empty means "no owner filter".

    Repeatable since MEMNG-133: the filter sheet asks for several household members at once, and
    the answer is the UNION of their visible-to-caller memos. A single owner is still spelled the
    old way (a bare string), and lands on the same one-element set, so nothing about a one-owner
    request changes — including an empty string, which named nobody before and still filters
    nothing rather than matching a user whose subject is "".

    Deduplicated, order preserved: `owner=alice&owner=alice` is one member asked for twice, not a
    reason to return their memos twice — the SQL `IN` would collapse it anyway, and doing it here
    means the statement the database sees is the same one either way.
    """
    if owner is None:
        return []
    values = [owner] if isinstance(owner, str) else list(owner)
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


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
    moved_out: list[storage.TrackedMove] | None = None,
    idempotency_key: str | None = None,
) -> Memo:
    memo = Memo(
        uid=gen_uid(),
        owner_id=owner.id,
        content=content,
        visibility=visibility,
        pinned=pinned,
        idempotency_key=idempotency_key,
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
    db: AsyncSession,
    att: Attachment,
    dest_dir: Path,
    data_root: Path,
    moved_out: list[storage.TrackedMove] | None,
) -> None:
    """Move this attachment's still-staged masters (the screen original + each hidden rendition)
    into the memo folder under their real names. ONLY a master in _unfiled/ is moved — an existing
    (committed) master is left in place (new-writes-only). Each move is tracked in `moved_out` so a
    rolled-back post can put it back where it came from — the record is appended BEFORE the move
    starts and completed inside the worker thread, so a cancelled request cannot lose it
    (MEMNG-145; see `storage.TrackedMove`)."""
    if att.storage_path and storage.is_unfiled(att.storage_path, data_root):
        staged = att.storage_path  # read before the reassignment: this is where it came FROM
        track = storage.TrackedMove(src=staged)
        if moved_out is not None:
            moved_out.append(track)
        new = await storage.place_master(Path(staged), dest_dir, att.filename, track)
        att.storage_path = str(new)
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
            staged = r.storage_path
            track = storage.TrackedMove(src=staged)
            if moved_out is not None:
                moved_out.append(track)
            new = await storage.place_master(Path(staged), dest_dir, r.filename, track)
            r.storage_path = str(new)


async def link_attachments(
    db: AsyncSession,
    owner_id: int,
    memo: Memo,
    uids: Sequence[str],
    moved_out: list[storage.TrackedMove] | None = None,
) -> None:
    """Link the caller's OWN attachments to a memo, IN THE ORDER `uids` names them.

    A uid belonging to another user is silently ignored so a memo can never claim someone else's
    media. Used on create and on edit. As each attachment is linked, its still-staged masters are
    placed into the memo's per-memo folder.

    ORDER (MEMNG-262). `uids` is the client's declared order, and each newly linked attachment is
    stamped with its RANK in it — `Attachment.position`, which the memo's `attachments` relation
    sorts by (NULLS LAST, then id). Rank, not the raw index in the request: a uid can name no new
    attachment of its own (a print master binds onto its screen as a rendition) or name one another
    uid already named (dedup), and a hole left where such a uid sat would make the column mean
    "index into a request nobody keeps" rather than "place in this memo" — which is also the only
    reading under which the append rule below (continue from max + 1) stays consistent.

    New attachments go AFTER whatever the memo already carries,
    because an existing memo's saved order is a fact and opening it for an edit must not re-sort
    it. Two shapes of "whatever it already carries":

    * every existing attachment already numbered → continue from `max + 1`;
    * ANY of them still NULL (a memo from before the column, or a mix of the two, which a PATCH
      can produce by adding files to a memo that also holds legacy rows) → renumber ALL of the
      memo's existing attachments 0..k-1 in their CURRENT DISPLAYED order first, then append. Only
      numbering the NULL ones would sort them ahead of the numbered ones (NULLS LAST means the
      numbers win), which would reorder attachments somebody has already seen.

    An attachment already linked to this memo keeps its place; one linked to a DIFFERENT memo is
    skipped and consumes no position. It all happens in the caller's transaction, so the renumber
    and the append land together or not at all.
    """
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
    # Read BEFORE anything is linked: these are the attachments whose order is already a fact.
    # Locked in id order (a stable lock order), then sorted the way they are displayed.
    already = (
        (
            await db.execute(
                select(Attachment)
                .where(Attachment.memo_id == memo.id)
                .order_by(Attachment.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    next_position = 0
    if already:
        if any(a.position is None for a in already):
            displayed = sorted(already, key=lambda a: (a.position is None, a.position or 0, a.id))
            for i, a in enumerate(displayed):
                a.position = i
            next_position = len(displayed)
        else:
            next_position = max(a.position or 0 for a in already) + 1
    by_uid = {a.uid: a for a in owned}
    linked: set[int] = {a.id for a in already}
    for uid in uids:  # the DECLARED order — `owned` is in id order and cannot carry it
        att = by_uid.get(uid)
        # Never STEAL an attachment already linked to a different memo — that would strand the
        # original memo (the defence for the back-compat `attachment_uids` path; the dedup path is
        # also guarded at the source in uploads.finalize). Re-linking to the same memo is a no-op.
        if att is None or (att.memo_id is not None and att.memo_id != memo.id):
            continue
        if att.id in linked:  # the same uid twice, or one already on this memo: one place only
            continue
        linked.add(att.id)
        att.memo_id = memo.id
        att.position = next_position
        next_position += 1
        await _place_attachment_masters(db, att, dest_dir, data_root, moved_out)
    await db.flush()


async def get_by_idempotency_key(db: AsyncSession, owner_id: int, key: str) -> Memo | None:
    """The memo this owner already stored under `key`, or None (MEMNG-110).

    Scoped to the owner because the key is the CLIENT's name for a create, not a global one: two
    users retrying with the same token must each get their own memo, which is also exactly what the
    partial unique index enforces.

    FOR SHARE on the memo row, because a replay must answer with the WHOLE memo. The relationships
    are `selectin`, so reading one memo is several statements, and under READ COMMITTED each of
    them sees a newer snapshot than the last: a DELETE committing midway through would otherwise
    hand back a 201 carrying a memo with some or none of its attachments, tags and reactions — a
    lie no client could detect. A share lock is enough to stop exactly that, and no more: deleting
    the memo has to take a conflicting lock on this same parent row before its cascade can remove
    any child, so a concurrent delete serializes behind this read instead of tearing it in half.
    Two replays of one key still run concurrently, since share locks do not exclude each other."""
    return (
        await db.execute(
            select(Memo)
            .where(Memo.owner_id == owner_id, Memo.idempotency_key == key)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()


async def get_memo_by_edit_key(db: AsyncSession, owner_id: int, key: str) -> Memo | None:
    """The memo this owner's edit `key` was already applied to, or None (MEMNG-124).

    The answer is the memo's CURRENT state, deliberately — the recorded row says THAT the edit
    landed, never what the row looked like afterwards. A replay arriving after some later write is
    therefore answered with the memo as it stands now, which is both the only thing obtainable
    without storing response bodies and what a retrying client is asking: "did my edit land, and
    what does the memo say now?".

    Scoped to the owner because the key is the CLIENT's name for its own edit, not a global one —
    the same scope the unique constraint enforces. The memo it returns may belong to a DIFFERENT
    memo than the one being patched (a client reusing one key across two memos); resolving that is
    the router's job, because only it knows which memo the request addressed.

    FOR SHARE on the memo row alone (`of=Memo` — the join row must not be locked, and locking it
    would serialize replays against each other for nothing), for the reason get_by_idempotency_key
    documents: the relationships are `selectin`, so reading one memo is several statements, each
    seeing a newer READ COMMITTED snapshot than the last, and a DELETE committing midway would hand
    back a 200 carrying a memo with some or none of its attachments. A share lock stops exactly
    that — a delete must take a conflicting lock on this same parent row before its cascade can
    remove any child — and no more: share locks do not exclude each other, so two replays of one
    key still run concurrently.
    """
    return (
        await db.execute(
            select(Memo)
            .join(MemoEditKey, MemoEditKey.memo_id == Memo.id)
            .where(MemoEditKey.owner_id == owner_id, MemoEditKey.edit_key == key)
            .with_for_update(read=True, of=Memo)
        )
    ).scalar_one_or_none()


async def record_edit_key(db: AsyncSession, memo: Memo, owner_id: int, key: str) -> None:
    """Record that `key` has been applied to this memo, in the caller's transaction (MEMNG-124).

    Flushed here rather than left to the commit so the unique violation — the settlement for two
    patches racing with one key — surfaces at a point the caller can still handle, and surfaces
    EARLY: called before the edit does any work, the loser blocks on the index and fails before it
    has moved a single upload's bytes, so there is nothing to move back.

    Not committed here: the row and the edit it names must land together or not at all. A recorded
    key whose edit rolled back would answer the client's honest retry with "already applied" for an
    edit that never happened."""
    db.add(MemoEditKey(memo_id=memo.id, owner_id=owner_id, edit_key=key))
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
    owner: str | Sequence[str] | None = None,
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
    subjects = owner_subjects(owner)
    if subjects:
        conds.append(Memo.owner_id.in_(select(User.id).where(User.subject.in_(subjects))))
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
    # `is not None`, not truthiness: `?cursor=` (sent empty) used to fall through to page 1, which
    # is the loop-for-ever failure MEMNG-163 is about — an empty token is a bad token, not "start
    # over". Callers that page internally (the export walk) still pass None for the first page.
    if cursor is not None:
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


async def delete_memo(db: AsyncSession, memo: Memo) -> list[int]:
    """Delete a memo, ORPHANING its attachments; returns their ids for the caller to reclaim after
    this transaction commits.

    Two phases, and the split is the whole point. Deleting the memo alone was the MEMNG-170 bug —
    the relationship carries no cascade and the FK is ON DELETE SET NULL, so every attachment
    survived with `memo_id` NULL, kept appearing in the media library, and kept its files. Deleting
    the rows HERE instead, in this transaction, trades that for something worse: a crash between
    this commit and the caller's unlink leaves files with no row at all, and nothing can ever find
    them again (`app/services/reclaim.py`).

    So this phase deliberately leaves the rows behind, orphaned. That orphan IS the durable
    journal: it names its own files, and it is exactly what `reclaim.leaked_orphans_stmt` selects,
    so a create interrupted here is picked up by the worker sweep or the reconcile script rather
    than lost. The caller then reclaims each id properly — locked, revalidated, files first.

    Reclaim is unconditional, with no reference counting, because an attachment on a memo is
    exclusively that memo's: `uploads.finalize` only ever dedups onto an UNLINKED attachment
    (`memo_id IS NULL`), and `link_attachments` refuses to re-point one that already belongs to a
    different memo. The same bytes posted to a second memo therefore get their own row and their
    own file, which this delete does not touch.

    The FK stays SET NULL rather than becoming CASCADE, and now for a second reason: CASCADE would
    make a raw DELETE against the database take the attachment rows with it and leave their files
    orphaned for ever — the forbidden outcome, reachable by hand from a psql prompt.
    """
    orphaned = [att.id for att in memo.attachments]
    await db.delete(memo)
    await db.flush()
    return orphaned


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
