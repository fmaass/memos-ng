"""Idempotent one-way ETL: old memos (0.28.1) DB → memos-ng DB.

Reads the old `memos` DB READ-ONLY and upserts users/memos/attachments/reactions into the
memos-ng DB. Re-runnable: keyed by uid (memos/attachments) and subject (users), so a second
run is a no-op. File bytes are copied separately on Saturn (cp -p); this records each
attachment's storage_path + links derived tiers, and enqueues a slow-lane job for any video
that has no derived mp4 yet (throttled by the worker's thermal guard).

The DB read/write is split from the import logic so the import is unit-tested with seeded
dicts against a temp memos-ng DB (no real old DB needed).

Usage:
  uv run python -m app.scripts.migrate_from_memos --source-dsn <old-dsn> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import (
    Attachment,
    AttachmentStatus,
    JobLane,
    JobState,
    MediaJob,
    Memo,
    Reaction,
    Tag,
    User,
    Visibility,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

VIDEO_PREFIX = "video/"


def epoch_to_dt(ts: int | float) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC)


def _role(old_role: str | None) -> str:
    return "admin" if (old_role or "").upper() in ("HOST", "ADMIN") else "user"


def _avatar(avatar_url: str | None) -> str | None:
    # memos stores avatars as data: URLs; keep only those (ignore external/empty).
    return avatar_url if avatar_url and avatar_url.startswith("data:image/") else None


def _get_or_create_tags(session: Session, names: Iterable[str]) -> list[Tag]:
    out: list[Tag] = []
    for name in names:
        tag = session.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            session.flush()
        out.append(tag)
    return out


def import_user(session: Session, old: dict[str, Any]) -> User:
    """Upsert by subject (= old username = Authelia preferred_username)."""
    subject = old["username"]
    user = session.execute(select(User).where(User.subject == subject)).scalar_one_or_none()
    avatar = _avatar(old.get("avatar_url"))
    theme = old.get("theme") or "dark"
    default_vis = Visibility(old.get("default_visibility") or "PUBLIC")
    if user is None:
        user = User(
            subject=subject,
            display_name=old.get("nickname") or None,
            email=old.get("email") or None,
            role=_role(old.get("role")),
            avatar=avatar,
            theme=theme,
            default_visibility=default_vis,
        )
        session.add(user)
    else:
        user.display_name = old.get("nickname") or user.display_name
        user.email = old.get("email") or user.email
        user.role = _role(old.get("role"))
        if avatar:
            user.avatar = avatar
        user.theme = theme
        user.default_visibility = default_vis
    session.flush()
    return user


def import_memo(session: Session, old: dict[str, Any], owner: User) -> Memo:
    """Upsert by uid (idempotent skip if it already exists)."""
    memo = session.execute(select(Memo).where(Memo.uid == old["uid"])).scalar_one_or_none()
    if memo is not None:
        return memo
    archived = (old.get("row_status") or "NORMAL").upper() == "ARCHIVED"
    memo = Memo(
        uid=old["uid"],
        owner_id=owner.id,
        content=old["content"],
        visibility=Visibility(old["visibility"]),
        pinned=bool(old.get("pinned", False)),
        created_at=epoch_to_dt(old["created_ts"]),
        updated_at=epoch_to_dt(old["updated_ts"]),
        archived_at=epoch_to_dt(old["updated_ts"]) if archived else None,
    )
    payload = old.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    memo.tags = _get_or_create_tags(session, payload.get("tags") or [])
    session.add(memo)
    session.flush()
    return memo


def _has_derived(uid: str, kind: str, ext: str) -> bool:
    return (Path(settings.DATA_ROOT) / "assets" / kind / f"{uid}.{ext}").exists()


def import_attachment(
    session: Session, old: dict[str, Any], owner: User, memo: Memo | None
) -> Attachment:
    """Upsert by uid. storage_path mirrors the old `reference` under originals/. Sets ready
    when its derived tiers already exist; otherwise enqueues processing (videos w/o an mp4
    get a throttled slow-lane job)."""
    att = session.execute(
        select(Attachment).where(Attachment.uid == old["uid"])
    ).scalar_one_or_none()
    if att is not None:
        return att
    uid = old["uid"]
    mime = old.get("type") or "application/octet-stream"
    is_video = mime.startswith(VIDEO_PREFIX)
    # old `reference` is an absolute container path (/var/opt/memos/assets/<basename>); the
    # file is copied flat into originals/ keyed by that unique basename.
    storage_path = str(Path(settings.DATA_ROOT) / "originals" / Path(old["reference"]).name)

    image_ready = not is_video and _has_derived(uid, "thumbs", "jpg")
    video_ready = is_video and _has_derived(uid, "web", "mp4")
    status = (
        AttachmentStatus.ready
        if (image_ready or video_ready)
        else (AttachmentStatus.processing if is_video else AttachmentStatus.pending)
    )

    att = Attachment(
        uid=uid,
        owner_id=owner.id,
        memo_id=memo.id if memo is not None else None,
        filename=old.get("filename") or uid,
        mime=mime,
        size=int(old.get("size") or 0),
        status=status,
        storage_path=storage_path,
    )
    session.add(att)
    session.flush()

    # No existing derived media → schedule it (fast lane for images, slow lane for videos).
    if status in (AttachmentStatus.pending, AttachmentStatus.processing):
        session.add(MediaJob(attachment_id=att.id, lane=JobLane.fast, state=JobState.queued))
        if is_video and not video_ready:
            session.add(MediaJob(attachment_id=att.id, lane=JobLane.slow, state=JobState.queued))
        session.flush()
    return att


def import_reaction(session: Session, memo_uid: str, subject: str, emoji: str) -> None:
    memo = session.execute(select(Memo).where(Memo.uid == memo_uid)).scalar_one_or_none()
    user = session.execute(select(User).where(User.subject == subject)).scalar_one_or_none()
    if memo is None or user is None:
        return
    existing = session.execute(
        select(Reaction).where(
            Reaction.memo_id == memo.id, Reaction.user_id == user.id, Reaction.emoji == emoji
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(Reaction(memo_id=memo.id, user_id=user.id, emoji=emoji))
        session.flush()


def import_all(session: Session, data: dict[str, Any]) -> dict[str, int]:
    """Import a normalized old-memos dataset. `data` keys: users, memos, attachments,
    reactions — each a list of dicts. memos/attachments reference users by old `id`."""
    by_old_id: dict[int, User] = {}
    for ou in data.get("users", []):
        by_old_id[ou["id"]] = import_user(session, ou)

    memo_by_old_id: dict[int, Memo] = {}
    for om in data.get("memos", []):
        owner = by_old_id[om["creator_id"]]
        memo_by_old_id[om["id"]] = import_memo(session, om, owner)

    for oa in data.get("attachments", []):
        owner = by_old_id[oa["creator_id"]]
        memo = memo_by_old_id.get(oa["memo_id"]) if oa.get("memo_id") else None
        import_attachment(session, oa, owner, memo)

    for r in data.get("reactions", []):
        import_reaction(session, r["memo_uid"], r["subject"], r["emoji"])

    return {
        "users": len(data.get("users", [])),
        "memos": len(data.get("memos", [])),
        "attachments": len(data.get("attachments", [])),
        "reactions": len(data.get("reactions", [])),
    }


def read_old(source_dsn: str) -> dict[str, Any]:  # pragma: no cover - exercised against real DB
    """Read the old memos DB READ-ONLY into the normalized dataset import_all expects."""
    import psycopg2

    conn = psycopg2.connect(source_dsn)
    conn.set_session(readonly=True)
    out: dict[str, Any] = {"users": [], "memos": [], "attachments": [], "reactions": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, email, nickname, avatar_url FROM \"user\" "
                "WHERE row_status = 'NORMAL'"
            )
            user_cols = ("id", "username", "role", "email", "nickname", "avatar_url")
            users_by_id = {}
            for r in cur.fetchall():
                u = dict(zip(user_cols, r, strict=False))
                users_by_id[u["id"]] = u
                out["users"].append(u)
            for uid, u in users_by_id.items():
                cur.execute("SELECT key, value FROM user_setting WHERE user_id = %s", (uid,))
                for key, value in cur.fetchall():
                    val = json.loads(value) if value else {}
                    if key in ("GENERAL", "MEMO"):
                        u.setdefault("theme", val.get("theme"))
                        u.setdefault(
                            "default_visibility",
                            val.get("memoVisibility") or val.get("defaultVisibility"),
                        )
            cur.execute(
                "SELECT id, uid, creator_id, created_ts, updated_ts, row_status, content, "
                "visibility, pinned, payload FROM memo"
            )
            cols = (
                "id", "uid", "creator_id", "created_ts", "updated_ts", "row_status",
                "content", "visibility", "pinned", "payload",
            )
            out["memos"] = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
            cur.execute(
                "SELECT id, uid, creator_id, filename, type, size, memo_id, reference "
                "FROM attachment"
            )
            acols = ("id", "uid", "creator_id", "filename", "type", "size", "memo_id", "reference")
            out["attachments"] = [dict(zip(acols, r, strict=False)) for r in cur.fetchall()]
            # reaction.content_id is "memos/{uid}"; reaction_type is the emoji.
            cur.execute("SELECT content_id, creator_id, reaction_type FROM reaction")
            for content_id, creator_id, rtype in cur.fetchall():
                subject = users_by_id.get(creator_id, {}).get("username")
                if subject:
                    out["reactions"].append(
                        {
                            "memo_uid": str(content_id).split("memos/")[-1],
                            "subject": subject,
                            "emoji": rtype,
                        }
                    )
    finally:
        conn.close()
    return out


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(description="Migrate old memos data into memos-ng")
    ap.add_argument("--source-dsn", required=True, help="old memos DB DSN (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="print counts, write nothing")
    args = ap.parse_args()

    data = read_old(args.source_dsn)
    counts = {k: len(v) for k, v in data.items()}
    print(f"source counts: {counts}")
    if args.dry_run:
        print("dry-run: no writes")
        return

    engine = create_engine(settings.DATABASE_URL_SYNC)
    with Session(engine) as session:
        result = import_all(session, data)
        session.commit()
    print(f"imported: {result}")


if __name__ == "__main__":  # pragma: no cover
    main()
