"""Trip passes, the home side (plan PLAN_2026-10-27_trip-pass-pluto P1).

Home is the authority for a pass: it is issued, edited and revoked here and pushed to the ingest
host, which enrolls guest devices. Three rules hold everywhere in this module:

* The secret is shown once. It is `secrets.token_urlsafe(32)` and only its sha256 is stored, the
  way a PAT is (`app.auth.hash_token`).
* `Pass.version` is the epoch. Every change raises it while the pass row is held FOR UPDATE, so two
  overlapping changes take turns on the row and each sees the other's result. A revoke is ONE
  terminal transition stamped with the version that performed it; a change that queued behind it
  finds the pass revoked under the lock and is refused (`PassRevoked`), never applied over it.
* Guest text is inert. `create_guest_memo` is the only place a guest batch becomes a memo, and the
  memo's `content` — the one field `parse_tags` reads — is built from the home collection row
  alone (`guest_memo_content`). A guest's name and caption go to their own columns.
"""

import io
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import segno
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_token
from app.config import settings
from app.models import (
    Attachment,
    Collection,
    CollectionExclusion,
    CollectionMember,
    Memo,
    Pass,
    PassDevice,
    PassEvent,
    PassSyncState,
    User,
    Visibility,
)
from app.services import collections as coll_svc
from app.services import memos as memos_svc
from app.services import reclaim, storage

DEFAULT_MAX_DEVICES = 30
SPAN_GRACE = timedelta(days=14)
SPANLESS_LIFETIME = timedelta(days=30)
CONTRIBUTOR_NAME_MAX = 64


class TripHasNoTag(Exception):
    """The trip has no tag a guest memo could carry unchanged through `parse_tags`."""


class PassRevoked(Exception):
    """The pass was revoked; the transition is terminal."""


class DeviceRevoked(Exception):
    """The device was already revoked."""


class PassGone(Exception):
    """The pass, its collection or its issuer no longer exists."""


async def _before_pass_lock() -> None:
    """A test seam: the instant between a route's lookup and the row lock, where two overlapping
    requests must both be able to stand so the lock — not the read — is shown to decide."""


TRIP_TAG_MAX = 255  # collections.tag is String(255)
_UID_FALLBACK_LEN = 8


def derive_trip_tag(title: str, uid: str) -> str:
    """The tag a trip created from dates alone is given at its first pass (MEMNG-1057, the owner's
    rule): the title lower-cased with every character that is not a letter or digit removed —
    "Cyprus 2026" is `cyprus2026`. No uniqueness check and no suffix, on purpose: memos already
    carrying that tag join the trip (membership is the tag OR the span), which is what a trip named
    Cyprus 2026 is for. Lower-casing comes first because it can itself produce a non-letter (the
    combining dot of "İ"). A title that leaves nothing, or nothing `parse_tags` reads back whole,
    falls back to `trip` and the uid's first eight letters and digits."""
    squashed = "".join(ch for ch in title.lower() if ch.isalnum())[:TRIP_TAG_MAX]
    if squashed and memos_svc.parse_tags(f"#{squashed}") == [squashed]:
        return squashed
    uid_part = "".join(ch for ch in uid if ch.isascii() and ch.isalnum())[:_UID_FALLBACK_LEN]
    return f"trip{uid_part.lower()}"


async def _tag_trip_if_untagged(db: AsyncSession, collection: Collection) -> Collection:
    """FOR UPDATE on the trip row and re-read it, then give it `derive_trip_tag` when — AS IT IS
    NOW — it has no tag. Under the lock, two overlapping issuances take turns and the second sees
    the first's tag, and a tag another writer committed meanwhile is kept rather than overwritten.
    An existing tag is never replaced, even one `parse_tags` cannot read (`guest_memo_content`
    refuses that one). The CHECKs hold: a trip with a tag needs nothing else."""
    await _before_pass_lock()
    locked = (
        await db.execute(
            select(Collection)
            .where(Collection.id == collection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise coll_svc.CollectionGone(collection.id)
    if locked.tag is None:
        locked.tag = derive_trip_tag(locked.title, locked.uid)
        await db.flush()
    return locked


def guest_memo_content(collection: Collection) -> str:
    """The ENTIRE `content` of a memo a guest batch becomes: `#<tag>` for a trip, empty for an album
    (whose membership is its member rows). Built from the home row only; raises `TripHasNoTag`
    when the tag would not come back out of `parse_tags` exactly as stored."""
    if collection.kind == "album":
        return ""
    tag = collection.tag
    if not tag or memos_svc.parse_tags(f"#{tag}") != [tag]:
        raise TripHasNoTag(collection.uid)
    return f"#{tag}"


class DoorNotConfigured(Exception):
    """`TRIP_PASS_BASE_URL` is not an absolute https origin, so no pass URL can be minted."""


def door_origin() -> str:
    """The configured guest-door origin, checked: `https://host[:port]` and nothing else. An empty
    or relative value would print `/p/<secret>` into the answer and the QR code — a pass nobody can
    use, with its secret already shown — so issuance asks this BEFORE anything is written."""
    raw = settings.TRIP_PASS_BASE_URL
    try:
        parts = urlsplit(raw)
        _ = parts.port  # a non-numeric or out-of-range port raises only when it is read
    except ValueError as exc:  # also an unbalanced IPv6 bracket
        raise DoorNotConfigured(raw) from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise DoorNotConfigured(raw)
    return f"https://{parts.netloc}"


def pass_url(secret: str) -> str:
    return f"{door_origin()}/p/{secret}"


def qr_svg(url: str) -> str:
    """A standalone SVG (with its namespace) of `url` as a QR code, error correction M."""
    out = io.BytesIO()
    segno.make(url, error="m").save(
        out, kind="svg", xmldecl=False, svgclass=None, lineclass=None, scale=4, border=4
    )
    return out.getvalue().decode()


def pass_state(p: Pass, now: datetime | None = None) -> Literal["active", "expired", "revoked"]:
    if p.revoked_at is not None or p.collection_id is None:
        return "revoked"
    if p.expires_at <= (now or datetime.now(UTC)):
        return "expired"
    return "active"


def default_expiry(collection: Collection, now: datetime) -> datetime:
    if collection.span_end is not None:
        return collection.span_end + SPAN_GRACE
    return now + SPANLESS_LIFETIME


async def _event(
    db: AsyncSession,
    p: Pass,
    kind: str,
    *,
    actor: User | None = None,
    device_id: int | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        PassEvent(
            pass_id=p.id,
            kind=kind,
            version=p.version,
            actor_id=actor.id if actor is not None else None,
            device_id=device_id,
            detail=detail,
        )
    )


async def _request_push(db: AsyncSession, pass_id: int, *, urgent: bool) -> None:
    """Mark a push of this pass owed. `urgent` (a revocation) is never downgraded by a later
    ordinary change: only the sync that delivers it clears it."""
    now = func.now()
    stmt = pg_insert(PassSyncState).values(
        pass_id=pass_id, push_requested_at=now, push_urgent=urgent
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PassSyncState.pass_id],
        set_={
            "push_requested_at": now,
            "push_urgent": PassSyncState.push_urgent | stmt.excluded.push_urgent,
        },
    )
    await db.execute(stmt)


async def issue_pass(
    db: AsyncSession,
    collection: Collection,
    issuer: User,
    *,
    label: str,
    scope: str = "contribute",
    expires_at: datetime | None = None,
    max_devices: int = DEFAULT_MAX_DEVICES,
    notes: str | None = None,
) -> tuple[Pass, str]:
    """Issue a pass on `collection`; returns `(row, secret)`. The secret exists only in this
    return value. A guest picture taken outside the span joins a trip only by its tag, so a trip
    without one is given one here, in this transaction (`_tag_trip_if_untagged`); a trip whose
    existing tag would not survive `parse_tags` raises `TripHasNoTag` before anything is written.
    A vanished collection raises `CollectionGone`."""
    door_origin()
    if collection.kind == "trip":
        collection = await _tag_trip_if_untagged(db, collection)
    guest_memo_content(collection)
    now = datetime.now(UTC)
    secret = secrets.token_urlsafe(32)
    p = Pass(
        uid=memos_svc.gen_uid(),
        collection_id=collection.id,
        secret_hash=hash_token(secret),
        label=label,
        scope=scope,
        expires_at=expires_at or default_expiry(collection, now),
        max_devices=max_devices,
        notes=notes,
        created_by=issuer.id,
        version=1,
    )
    db.add(p)
    await db.flush()
    await _event(db, p, "issued", actor=issuer)
    await _request_push(db, p.id, urgent=False)
    await db.flush()
    await db.refresh(p)
    return p, secret


async def get_pass(db: AsyncSession, uid: str) -> Pass | None:
    return (await db.execute(select(Pass).where(Pass.uid == uid))).scalar_one_or_none()


async def list_passes(db: AsyncSession, collection: Collection) -> list[Pass]:
    stmt = (
        select(Pass)
        .where(Pass.collection_id == collection.id)
        .order_by(Pass.created_at.desc(), Pass.id.desc())
    )
    return list((await db.execute(stmt)).scalars())


async def _lock_pass(db: AsyncSession, pass_id: int) -> Pass:
    """FOR UPDATE on the pass row: every change to a pass and its devices takes turns here, so the
    version it writes is the one after the previous change committed. A revoked pass refuses."""
    await _before_pass_lock()
    locked = (
        await db.execute(
            select(Pass)
            .where(Pass.id == pass_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise PassGone(pass_id)
    if locked.revoked_at is not None:
        raise PassRevoked(locked.uid)
    return locked


_EDITABLE = ("label", "scope", "expires_at", "max_devices", "notes")


async def update_pass(db: AsyncSession, p: Pass, actor: User, changes: dict[str, object]) -> Pass:
    """Apply `changes` (a subset of the editable fields). A change that alters nothing writes
    nothing and keeps the version; anything else raises it by one and owes a push."""
    locked = await _lock_pass(db, p.id)
    changed = {k: v for k, v in changes.items() if k in _EDITABLE and getattr(locked, k) != v}
    if not changed:
        return locked
    for key, value in changed.items():
        setattr(locked, key, value)
    locked.version += 1
    await _event(db, locked, "updated", actor=actor, detail=",".join(sorted(changed)))
    await _request_push(db, locked.id, urgent=False)
    await db.flush()
    await db.refresh(locked)
    return locked


async def revoke_pass(db: AsyncSession, p: Pass, actor: User) -> Pass:
    """The terminal transition: stamp the revocation with the version that performs it, revoke
    every device with it, and owe an IMMEDIATE push. A second revoke is refused (`PassRevoked`)."""
    locked = await _lock_pass(db, p.id)
    await _revoke_locked(db, locked, actor)
    await db.flush()
    await db.refresh(locked)
    return locked


async def _revoke_locked(
    db: AsyncSession, locked: Pass, actor: User | None, *, detail: str | None = None
) -> None:
    """The revoke transition on a pass row the caller holds FOR UPDATE and has seen live."""
    locked.version += 1
    now = datetime.now(UTC)
    locked.revoked_at = now
    locked.revoked_version = locked.version
    locked.revoked_by = actor.id if actor is not None else None
    await db.execute(
        update(PassDevice)
        .where(PassDevice.pass_id == locked.id, PassDevice.revoked_at.is_(None))
        .values(revoked_at=now, revoked_version=locked.version)
    )
    await _event(db, locked, "revoked", actor=actor, detail=detail)
    await _request_push(db, locked.id, urgent=True)


async def revoke_collection_passes(db: AsyncSession, collection_id: int, actor: User | None) -> int:
    """Revoke every pass of a collection that is about to be deleted; returns how many.

    The FK's SET NULL alone would leave the pass dead at home (no collection reads as revoked) but
    with no version moved and no urgent push, so the door would keep accepting it. The caller holds
    the collection row FOR UPDATE, which blocks an issue racing the delete (its insert takes KEY
    SHARE on that row), so the set read here is the whole set. Lock order C → P, ascending ids in
    one statement (docs/LOCK-ORDER.md)."""
    rows = (
        await db.execute(
            select(Pass)
            .where(Pass.collection_id == collection_id, Pass.revoked_at.is_(None))
            .order_by(Pass.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    revoked = 0
    for locked in rows:
        await _revoke_locked(db, locked, actor, detail="collection_deleted")
        revoked += 1
    return revoked


async def list_devices(db: AsyncSession, p: Pass) -> list[PassDevice]:
    stmt = (
        select(PassDevice)
        .where(PassDevice.pass_id == p.id)
        .order_by(PassDevice.first_seen, PassDevice.id)
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(stmt)).scalars())


async def revoke_device(
    db: AsyncSession, p: Pass, device_uid: str, actor: User
) -> PassDevice | None:
    """Revoke one device of `p`; None when `p` has no such device. Taken under the PASS lock, so a
    device revocation and a pass revocation never interleave, and the pass's version moves."""
    locked = await _lock_pass(db, p.id)
    device = (
        await db.execute(
            select(PassDevice).where(PassDevice.pass_id == locked.id, PassDevice.uid == device_uid)
        )
    ).scalar_one_or_none()
    if device is None:
        return None
    if device.revoked_at is not None:
        raise DeviceRevoked(device_uid)
    locked.version += 1
    device.revoked_at = datetime.now(UTC)
    device.revoked_version = locked.version
    await _event(db, locked, "device_revoked", actor=actor, device_id=device.id)
    await _request_push(db, locked.id, urgent=True)
    await db.flush()
    await db.refresh(device)
    return device


async def device_counts(db: AsyncSession, pass_ids: Sequence[int]) -> dict[int, int]:
    if not pass_ids:
        return {}
    rows = await db.execute(
        select(PassDevice.pass_id, func.count())
        .where(PassDevice.pass_id.in_(pass_ids))
        .group_by(PassDevice.pass_id)
    )
    return {pid: n for pid, n in rows.all()}


# --- contributions ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contribution:
    attachment_id: int
    attachment_uid: str
    memo_uid: str
    contributor_name: str | None
    device_uid: str | None
    caption: str | None
    hidden: bool
    created_at: datetime


def _hidden_clause(collection: Collection | None):
    if collection is None:
        return None
    if collection.kind == "album":
        return ~exists().where(
            CollectionMember.collection_id == collection.id,
            CollectionMember.attachment_id == Attachment.id,
        )
    return exists().where(
        CollectionExclusion.collection_id == collection.id,
        CollectionExclusion.attachment_id == Attachment.id,
    )


async def pass_collection(db: AsyncSession, p: Pass) -> Collection | None:
    if p.collection_id is None:
        return None
    return await db.get(Collection, p.collection_id)


async def list_contributions(db: AsyncSession, p: Pass) -> list[Contribution]:
    """Every live picture that came in through `p`, newest first, and whether it is hidden from
    the pass's collection. Readable after a revoke: moderation outlives the pass."""
    collection = await pass_collection(db, p)
    hidden = _hidden_clause(collection)
    stmt = (
        select(
            Attachment.id,
            Attachment.uid,
            Memo.uid,
            Attachment.contributor_name,
            PassDevice.uid,
            Memo.guest_caption,
            hidden if hidden is not None else func.false(),
            Attachment.created_at,
        )
        .join(Memo, Memo.id == Attachment.memo_id)
        .outerjoin(PassDevice, PassDevice.id == Attachment.contributed_by_device_id)
        .where(Memo.pass_id == p.id, Attachment.detached_at.is_(None))
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
    )
    return [Contribution(*row) for row in (await db.execute(stmt)).all()]


ModerationOutcome = Literal["hidden", "unhidden", "removed", "notContribution", "error"]


async def _contribution_ids(db: AsyncSession, p: Pass, uids: Sequence[str]) -> dict[str, int]:
    rows = await db.execute(
        select(Attachment.uid, Attachment.id)
        .join(Memo, Memo.id == Attachment.memo_id)
        .where(Attachment.uid.in_(uids), Memo.pass_id == p.id, Attachment.detached_at.is_(None))
    )
    return {uid: aid for uid, aid in rows.all()}


async def moderate(
    db: AsyncSession,
    p: Pass,
    actor: User,
    verb: Literal["hide", "unhide", "remove"],
    uids: Sequence[str],
) -> dict[str, ModerationOutcome]:
    """Hide, unhide or remove pictures that came in through `p`; one answer per uid. A uid that is
    not a live contribution of THIS pass is `notContribution` and nothing is done to it.

    Hiding reuses the collection's own record of "not in here": an exclusion row for a trip (one
    statement for the whole set), the member row taken away for an album. Removing is the
    attachment delete's seam (`reclaim.reclaim_row_shielded`, IDENTITY), one picture at a time."""
    wanted = list(dict.fromkeys(uids))
    outcomes: dict[str, ModerationOutcome] = dict.fromkeys(wanted, "notContribution")
    found = await _contribution_ids(db, p, wanted)
    collection = await pass_collection(db, p)
    # Plain values: the rollback below expires every loaded object, and a lazy load of one after it
    # is a MissingGreenlet in this async app.
    pass_id, actor_id = p.id, actor.id
    kind = collection.kind if collection is not None else None
    collection_id = collection.id if collection is not None else None
    targets = sorted((aid, uid) for uid, aid in found.items())
    await db.rollback()  # end the read; every write below opens its own transaction
    if verb == "remove":
        for aid, uid in targets:
            result = await reclaim.reclaim_row_shielded(aid, reclaim.Revalidate.IDENTITY)
            if result.outcome == "reclaimed":
                outcomes[uid] = "removed"
            elif result.outcome == "vanished":  # a concurrent delete took it first
                outcomes[uid] = "notContribution"
            else:  # its files could not be removed, so the row was kept (reclaim's rule)
                outcomes[uid] = "error"
    elif collection_id is None:
        pass  # the collection is gone: nothing to hide the pictures from
    elif kind == "album":
        for aid, uid in targets:
            try:
                if verb == "hide":
                    await coll_svc.remove_member(db, collection_id, aid)
                    outcomes[uid] = "hidden"
                else:
                    added = await coll_svc.add_member(db, collection_id, aid, added_by=actor_id)
                    outcomes[uid] = "notContribution" if added == "orphan" else "unhidden"
                await db.commit()
            except Exception:
                await db.rollback()
                outcomes[uid] = "error"
    elif targets:
        ids = [aid for aid, _ in targets]
        if verb == "hide":
            await db.execute(
                pg_insert(CollectionExclusion)
                .values([{"collection_id": collection_id, "attachment_id": a} for a in ids])
                .on_conflict_do_nothing(
                    index_elements=[
                        CollectionExclusion.collection_id,
                        CollectionExclusion.attachment_id,
                    ]
                )
            )
        else:
            await coll_svc.include_attachments(db, collection_id, ids)
        await db.commit()
        for _, uid in targets:
            outcomes[uid] = "hidden" if verb == "hide" else "unhidden"
    done = [uid for uid in wanted if outcomes[uid] in ("hidden", "unhidden", "removed")]
    if done:
        db.add_all(
            PassEvent(
                pass_id=pass_id,
                kind=outcomes[uid],
                actor_id=actor_id,
                detail=uid,
            )
            for uid in done
        )
        await db.commit()
    return outcomes


# --- the guest memo boundary -----------------------------------------------------------------


async def create_guest_memo(
    db: AsyncSession,
    p: Pass,
    *,
    caption: str | None,
    contributor_name: str | None,
    idempotency_key: str,
    device: PassDevice | None = None,
    finalize: Callable[[Memo], Awaitable[Sequence[str]]] | None = None,
    moved_out: list[storage.TrackedMove] | None = None,
) -> Memo:
    """The ONE place a guest batch becomes a home memo. Owner, visibility, tag and destination
    come from the pass's home rows only: the issuer owns it, it is PROTECTED, and its `content` is
    exactly `guest_memo_content` — so a guest's caption or name, whatever hashtags they carry,
    never reaches `parse_tags`. The caption is kept in `memos.guest_caption`, the name on each
    attachment's `contributor_name`. An album's member rows are the importer's to write."""
    collection = await pass_collection(db, p)
    if collection is None:
        raise PassGone(p.uid)
    content = guest_memo_content(collection)
    owner = await db.get(User, p.created_by) if p.created_by is not None else None
    if owner is None:
        raise PassGone(p.uid)
    memo = await memos_svc.create_memo(
        db,
        owner,
        content=content,
        visibility=Visibility.PROTECTED,
        idempotency_key=idempotency_key,
        finalize=finalize,
        # The final placement into the memo folder is a move like finalize's own, and a filing
        # that rolls back has to be able to walk it back too.
        moved_out=moved_out,
    )
    memo.pass_id = p.id
    memo.guest_caption = caption or None
    name = contributor_name[:CONTRIBUTOR_NAME_MAX] if contributor_name else None
    for att in memo.attachments:
        att.contributor_name = name
        att.contributed_by_device_id = device.id if device is not None else None
    await db.flush()
    return memo
