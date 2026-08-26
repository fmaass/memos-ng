"""Read-only memos 0.29.1-compatible API for unmigrated satellite clients (daily-mail,
slideshow, transcoder). Contract is the captured live fixtures in fixtures/live-0.29.1/.

Two ground-truth deviations from the original plan (see NOTES.md):
- F1: no/invalid token → HTTP 200 with PUBLIC-only memos (NOT 401). A PAT widens the set.
- F2: original files are NOT served over /file/* (404, as prod is today); only derived tiers
  (thumbnail / mobile-UA / video) are HTTP-served.

Derived tiers are served under the SAME visibility rules as the memo surfaces above, not
unconditionally (MEMNG-25): anonymous → PUBLIC memos only, a PAT → that user's visible set.
So a PUBLIC→PRIVATE flip, a memo deletion, or an attachment deletion revokes the media on the
next request, instead of leaving every derived tier world-readable by uid forever.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import settings
from app.database import async_session, get_session
from app.models import Attachment, Memo, Tag, User, Visibility
from app.services.cursors import encode_cursor_payload
from app.services.memos import decode_cursor, visible_clause

router = APIRouter(tags=["compat"])

VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".webm", ".avi", ".mkv")
TAG_IN_RE = re.compile(r'tag\s+in\s+\[([^\]]*)\]')
TAG_EQ_RE = re.compile(r'tag\s*==')


async def optional_user(request: Request, db: Annotated[AsyncSession, Depends(get_session)]):
    """The PAT user if a valid `Bearer memos_pat_…` is present, else None. No 401 — an
    anonymous (or bad-token) caller simply gets the PUBLIC-only result set (F1)."""
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer ") and authz[7:].startswith(auth.PAT_PREFIX):
        return await auth.authenticate_pat(db, authz[7:])
    return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _property(content: str) -> dict[str, Any]:
    return {
        "hasCode": "```" in content,
        "hasIncompleteTasks": "- [ ]" in content,
        "hasLink": "http://" in content or "https://" in content,
        "hasTaskList": bool(re.search(r"- \[[ xX]\]", content)),
        "title": "",
    }


def _compat_attachment(att, memo_name: str) -> dict[str, Any]:
    return {
        "name": f"attachments/{att.uid}",
        "createTime": _iso(att.created_at),
        "filename": att.filename,
        "content": "",  # always-empty in 0.29.1; clean API drops it
        "externalLink": "",
        "type": att.mime,
        "size": str(att.size),  # 0.29.1 serialises size as a STRING
        "memo": memo_name,
        "motionMedia": None,
    }


def _compat_memo(memo: Memo) -> dict[str, Any]:
    name = f"memos/{memo.uid}"
    return {
        "name": name,
        "state": "ARCHIVED" if memo.archived_at else "NORMAL",
        "creator": f"users/{memo.owner.subject}",
        "createTime": _iso(memo.created_at),
        "updateTime": _iso(memo.updated_at),
        "content": memo.content,
        "visibility": memo.visibility.value,
        "tags": sorted(t.name for t in memo.tags),
        "pinned": memo.pinned,
        "attachments": [_compat_attachment(a, name) for a in memo.attachments],
        "relations": [],
        "reactions": [
            {"emoji": r.emoji, "creator": f"users/{r.user_id}"} for r in memo.reactions
        ],
        "property": _property(memo.content),
        "snippet": memo.content[:200],
    }


def _parse_filter(filter_str: str | None) -> list[str]:
    """CEL filter → tag list. `tag in ["a","b"]` works; `tag == "a"` 400s (as 0.29.1 does)."""
    if not filter_str:
        return []
    if TAG_EQ_RE.search(filter_str):
        raise HTTPException(
            status_code=400,
            detail='invalid filter: field "tag" does not support comparison',
        )
    m = TAG_IN_RE.search(filter_str)
    if not m:
        return []
    return [s.strip().strip('"\'') for s in m.group(1).split(",") if s.strip()]


def _encode_token(memo: Memo) -> str:
    return encode_cursor_payload(f"{memo.created_at.isoformat()}|{memo.id}")


def _decode_token(token: str) -> tuple[datetime, int]:
    """Same keyset payload as the native feed's cursor, so it reuses that decode — a `pageToken`
    the server did not mint is a 422 here too, not the 500 it was before MEMNG-163."""
    return decode_cursor(token)


@router.get("/api/v1/memos")
async def list_memos_compat(
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User | None, Depends(optional_user)],
    pageSize: int = 10,
    pageToken: str | None = None,
    filter: str | None = None,
):
    tags = _parse_filter(filter)
    conds: list[Any] = [Memo.archived_at.is_(None)]
    # F1: anonymous → PUBLIC-only; a PAT user → their full visible set.
    conds.append(Memo.visibility == Visibility.PUBLIC if user is None else visible_clause(user))
    if tags:
        conds.append(Memo.tags.any(Tag.name.in_(tags)))
    if pageToken:
        ts, mid = _decode_token(pageToken)
        conds.append(tuple_(Memo.created_at, Memo.id) < (ts, mid))

    limit = max(1, min(pageSize, 100))
    stmt = (
        select(Memo)
        .where(*conds)
        .order_by(Memo.created_at.desc(), Memo.id.desc())
        .limit(limit + 1)
    )
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_token = _encode_token(page[-1]) if has_more and page else ""
    return {"memos": [_compat_memo(m) for m in page], "nextPageToken": next_token}


@router.get("/api/v1/memos/{memo_id}")
async def get_memo_compat(
    memo_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User | None, Depends(optional_user)],
):
    memo = (await db.execute(select(Memo).where(Memo.uid == memo_id))).scalar_one_or_none()
    if memo is None:
        raise HTTPException(status_code=404, detail="memo not found")
    visible = memo.visibility == Visibility.PUBLIC or (
        user is not None
        and (memo.owner_id == user.id or memo.visibility != Visibility.PRIVATE)
    )
    if not visible:
        raise HTTPException(status_code=404, detail="memo not found")
    return _compat_memo(memo)


async def file_visible(db: AsyncSession, user: User | None, uid: str) -> bool:
    """May `user` (None = anonymous) fetch a derived tier of attachment `uid` over /file/*?

    Mirrors the visibility rules already enforced elsewhere — it does not add a second model:
    - authenticated: `assets._viewable` — they own the attachment, OR it hangs on a memo
      `services.memos.visible_clause` lets them see (theirs, or non-PRIVATE).
    - anonymous: the F1 rule the compat memo surfaces use (list :129, get :159) — PUBLIC only.
      An orphan attachment (its memo was deleted → memo_id SET NULL) is therefore never
      anonymously readable, which is what makes deletion revoke access.
    A missing attachment row is a denial, not an error, so a deleted attachment 404s.
    """
    row = (
        await db.execute(
            select(Attachment.owner_id, Memo.id, Memo.visibility)
            .outerjoin(Memo, Memo.id == Attachment.memo_id)
            .where(Attachment.uid == uid)
        )
    ).first()
    if row is None:
        return False
    owner_id, memo_id, visibility = row
    if user is None:
        return memo_id is not None and visibility == Visibility.PUBLIC
    if owner_id is not None and owner_id == user.id:
        return True
    return memo_id is not None and visibility != Visibility.PRIVATE


@router.get("/file/attachments/{uid}/{filename}")
async def serve_file_compat(uid: str, filename: str, request: Request, thumbnail: bool = False):
    """Derived tiers only (F2): ?thumbnail=true → thumb, mobile UA → mobile, video → web.
    The original path stays 404, as prod is today. Visibility-gated like the memo surfaces:
    anonymous callers get derived tiers of PUBLIC memos only, a PAT widens to that user's
    visible set, and anything else (private, orphaned, deleted) is 404 — never 403, so the
    denial does not disclose that the uid exists."""
    assets = Path(settings.DATA_ROOT) / "assets"
    is_mobile = "Mobile" in request.headers.get("user-agent", "")
    is_video = filename.lower().endswith(VIDEO_EXTS)
    if thumbnail:
        path, media_type = assets / "thumbs" / f"{uid}.jpg", "image/jpeg"
    elif is_video:
        path, media_type = assets / "web" / f"{uid}.mp4", "video/mp4"
    elif is_mobile:
        path, media_type = assets / "mobile" / f"{uid}.jpg", "image/jpeg"
    else:
        # Original over /file/* is not served (F2).
        return JSONResponse({"message": "not found"}, status_code=404)
    # Authorize in a short-lived session that is released BEFORE the file is streamed, so this
    # high-fan-out media path never pins a pool connection for the length of a download (same
    # reason assets.py opens its own session instead of taking the request-scoped dependency).
    async with async_session() as db:
        user = await optional_user(request, db)
        allowed = await file_visible(db, user, uid)
    # Short-circuit preserved: a denied caller never costs a stat. The existence check itself
    # goes off-loop — this is a high-fan-out media route on the bind-mounted volume.
    if not allowed or not await to_thread.run_sync(path.exists):
        return JSONResponse({"message": "not found"}, status_code=404)
    # No shared cache may store these: the same URL is authorized per-caller now, and a
    # PUBLIC→PRIVATE flip must take effect on the very next request rather than being replayed
    # from a cache that outlives the flip.
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "private, no-store"}
    )
