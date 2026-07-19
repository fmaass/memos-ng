"""One-shot backfill: upload EVERY #curated screen photo into the aura-frames album.

#curated → Aura album delivery is event-driven: ``maybe_enqueue_frame_shares`` fires only when a
memo is created or edited, so every #curated memo that predates the album-upload integration
(memos-ng #31) — or whose photos were delivered by the now-retired SMTP-to-frame path — was never
uploaded to album ``FRAME_ALBUM_ID``. This script backfills them.

It reuses the worker's exact per-photo recipe (``frame_share.render_for_frame`` →
``upload_to_album``), so the "screen not print" and idempotency guarantees come for free:

* **Screen only.** A print master lives in ``Attachment.print_path`` / ``AttachmentRendition``
  (hidden, download-only, never an ``Attachment`` row), so a query over ``Attachment`` rows can
  never surface it. Only the screen file (``att.storage_path`` / ``att.mime``) is ever sent.
* **Idempotent.** aura-frames dedups on ``(album_id, sha256)`` and returns 200 for an
  already-present photo, so re-running — or racing the live worker — never duplicates a photo.

It uploads STRAIGHT to the album and deliberately does NOT consult ``frame_share_jobs``: photos
delivered by the old SMTP path read as ``sent`` there but were never in the album, and this puts
them in. (The album, not the job table, is the source of truth for "what is in the album".)

Default is a DRY RUN (counts only, uploads nothing). Pass ``--execute`` to actually upload. Run
inside the app container, which has the DB, the originals, and the album config::

    docker exec memos-ng-app python -m app.scripts.backfill_frame_album            # dry run
    docker exec memos-ng-app python -m app.scripts.backfill_frame_album --execute  # upload
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from anyio import to_thread
from app.database import async_session
from app.models import Attachment, Memo, Tag, memo_tags
from app.services.frame_share import (
    CURATED_TAG,
    frame_api_configured,
    is_frame_image,
    render_for_frame,
    upload_to_album,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# The render step is synchronous CPU work (Pillow); the upload step is an async HTTP POST. Both are
# injectable so the runner can be unit-tested without a filesystem/Pillow decode or a live album.
RenderFn = Callable[[str, datetime | None], bytes]
UploadFn = Callable[[str, bytes], Awaitable[None]]


@dataclass
class BackfillStats:
    curated_image_attachments: int = 0  # screen images considered across all #curated memos
    curated_memos_with_images: int = 0
    uploaded: int = 0  # aura-frames answered 200 (dup) or 201 (new) — the photo is in the album
    skipped_missing_file: int = 0  # storage_path absent or the file is gone on disk
    failed: int = 0  # a render or upload raised (per-item; the run continued)

    def format(self, *, execute: bool) -> str:
        mode = "EXECUTE" if execute else "DRY RUN (no uploads)"
        return (
            f"[{mode}] #curated screen photos: {self.curated_image_attachments} "
            f"across {self.curated_memos_with_images} memos | "
            f"uploaded={self.uploaded} skipped_missing_file={self.skipped_missing_file} "
            f"failed={self.failed}"
        )


async def load_curated_targets(db: AsyncSession) -> list[tuple[Memo, Attachment]]:
    """Every (#curated memo, screen-image attachment) pair, oldest memo first.

    The join is over ``Attachment`` rows only, so print masters (``Attachment.print_path`` /
    ``AttachmentRendition`` — never ``Attachment`` rows) cannot appear. ``is_frame_image`` then
    keeps exactly the screen-image set the worker enqueues (``image/*`` the frame renders; not
    video)."""
    rows = (
        await db.execute(
            select(Memo, Attachment)
            .join(memo_tags, memo_tags.c.memo_id == Memo.id)
            .join(Tag, Tag.id == memo_tags.c.tag_id)
            .join(Attachment, Attachment.memo_id == Memo.id)
            .where(Tag.name == CURATED_TAG)
            .order_by(Memo.created_at, Attachment.id)
        )
    ).all()
    return [(memo, att) for memo, att in rows if is_frame_image(att.mime)]


async def _upload_targets(
    targets: list[tuple[Memo, Attachment]],
    *,
    execute: bool,
    render: RenderFn,
    upload: UploadFn,
) -> BackfillStats:
    """Render + upload each target, isolating per-item failures so one bad photo never aborts the
    run. In dry-run mode nothing is rendered or uploaded — only counted (a missing file is still
    reported, since it would be skipped in a real run too)."""
    stats = BackfillStats(
        curated_image_attachments=len(targets),
        curated_memos_with_images=len({memo.id for memo, _ in targets}),
    )
    for memo, att in targets:
        if not att.storage_path or not await to_thread.run_sync(Path(att.storage_path).exists):
            stats.skipped_missing_file += 1
            logger.info("backfill.missing_file", memo_id=memo.id, attachment_id=att.id)
            continue
        if not execute:
            continue
        try:
            jpeg = await to_thread.run_sync(render, att.storage_path, memo.created_at)
            filename = Path(att.filename).stem + ".jpg"
            await upload(filename, jpeg)
            stats.uploaded += 1
            logger.info(
                "backfill.uploaded", memo_id=memo.id, attachment_id=att.id, bytes=len(jpeg)
            )
        except Exception as exc:  # per-item isolation: one bad photo never aborts the whole run
            stats.failed += 1
            logger.warning(
                "backfill.item_failed",
                memo_id=memo.id,
                attachment_id=att.id,
                error=str(exc)[:200],
            )
    return stats


async def run_backfill(
    db: AsyncSession,
    *,
    execute: bool,
    render: RenderFn = render_for_frame,
    upload: UploadFn = upload_to_album,
) -> BackfillStats:
    targets = await load_curated_targets(db)
    return await _upload_targets(targets, execute=execute, render=render, upload=upload)


async def _amain(*, execute: bool) -> BackfillStats:
    if execute and not frame_api_configured():
        raise SystemExit(
            "frame API not configured (FRAME_API_URL / FRAME_ALBUM_ID / FRAME_API_TOKEN) — "
            "run inside the memos-ng-app container. Refusing to upload."
        )
    async with async_session() as db:
        return await run_backfill(db, execute=execute)


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(
        description="Backfill all #curated screen photos into the aura-frames album (idempotent)."
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="actually upload to the album; the default is a dry run that only counts",
    )
    args = ap.parse_args()
    stats = asyncio.run(_amain(execute=args.execute))
    print(stats.format(execute=args.execute))


if __name__ == "__main__":  # pragma: no cover
    main()
