"""One-off backfill (and roll-forward replay): queue every currently hearted picture for the
favorites album.

The heart enqueues only from the moment the favorites feature ships, so the pictures hearted
before it — and any re-hearted while an image rollback ran without the enqueue — have no live job.
This script writes those jobs; it NEVER uploads. The worker drains them like any heart, spaced by
FRAME_FAVORITE_PACE_SECONDS, and records aura-frames' album photo id per job.

Unlike the curated backfill, the job table IS the ledger here: the favorites album starts empty, so
there is no old `sent` row whose photo the album lacks.

ONE idempotent upsert per eligible picture (the `favorites` picture tag present now, a frame image,
its original on disk):
  * no row yet -> inserted `queued`, due at once;
  * a `skipped` or `failed` row -> re-armed to `queued`, due now, attempts/error/lease cleared;
  * a `queued`, `claimed` or `sent` row -> never touched.
So a second run writes nothing, and a picture no longer hearted is never queued. `user_id` is the
picture's owner (the tag records nobody).

Default is a DRY RUN (counts only, writes nothing). Run inside the app container, after the album
exists and FRAME_FAVORITES_ALBUM_ID is set::

    docker exec memos-ng-app python -m app.scripts.backfill_favorites_frame            # dry run
    docker exec memos-ng-app python -m app.scripts.backfill_favorites_frame --execute  # enqueue
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from anyio import to_thread
from app.database import async_session
from app.models import Attachment, FavoriteFrameJob, FrameShareState, Tag, attachment_tags
from app.services.frame_share import FAVORITES_TAG, favorites_configured, is_frame_image
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

RESETTABLE = (FrameShareState.skipped, FrameShareState.failed)


@dataclass
class FavoritesBackfillStats:
    favorites: int = 0  # pictures carrying the `favorites` tag
    eligible: int = 0  # ...that are frame images with the original on disk
    no_memo: int = 0  # eligible pictures without a memo (sent all the same)
    missing_original: int = 0
    not_frame_image: int = 0
    existing_by_state: dict[str, int] = field(default_factory=dict)  # eligible pictures' rows
    would_insert: int = 0
    would_reset: int = 0
    inserted: int = 0  # written by --execute
    reset: int = 0

    def format(self, *, execute: bool) -> str:
        mode = "EXECUTE" if execute else "DRY RUN (no writes)"
        states = " ".join(f"{k}={v}" for k, v in sorted(self.existing_by_state.items())) or "none"
        return (
            f"[{mode}] favorites={self.favorites} eligible={self.eligible} "
            f"no_memo={self.no_memo} missing_original={self.missing_original} "
            f"not_frame_image={self.not_frame_image} | existing rows: {states} | "
            f"would_insert={self.would_insert} would_reset={self.would_reset} | "
            f"inserted={self.inserted} reset={self.reset}"
        )


async def load_favorites(db: AsyncSession) -> list[Attachment]:
    """Every picture carrying the `favorites` picture tag now, oldest first."""
    return list(
        (
            await db.execute(
                select(Attachment)
                .join(attachment_tags, attachment_tags.c.attachment_id == Attachment.id)
                .join(Tag, Tag.id == attachment_tags.c.tag_id)
                .where(Tag.name == FAVORITES_TAG)
                .order_by(Attachment.id)
            )
        )
        .scalars()
        .all()
    )


async def _eligible(atts: list[Attachment], stats: FavoritesBackfillStats) -> list[Attachment]:
    out: list[Attachment] = []
    for att in atts:
        if not is_frame_image(att.mime):
            stats.not_frame_image += 1
            continue
        if not att.storage_path or not await to_thread.run_sync(Path(att.storage_path).exists):
            stats.missing_original += 1
            logger.info("favorites_backfill.missing_original", attachment_id=att.id)
            continue
        out.append(att)
    return out


async def _upsert(db: AsyncSession, att: Attachment) -> bool:
    """The replay upsert for one eligible picture; True when it wrote (inserted or re-armed)."""
    ins = pg_insert(FavoriteFrameJob).values(
        attachment_id=att.id,
        user_id=att.owner_id,
        state=FrameShareState.queued,
        attempts=0,
        next_attempt_at=None,
    )
    result = await db.execute(
        ins.on_conflict_do_update(
            index_elements=["attachment_id"],
            set_={
                "state": FrameShareState.queued,
                "next_attempt_at": func.now(),
                "attempts": 0,
                "error": None,
                "claimed_by": None,
                "claimed_at": None,
            },
            where=FavoriteFrameJob.state.in_(RESETTABLE),
        ).returning(FavoriteFrameJob.id)
    )
    return result.first() is not None


async def backfill(db: AsyncSession, *, execute: bool) -> FavoritesBackfillStats:
    """Count, and with `execute` write, in the caller's session (the caller commits)."""
    stats = FavoritesBackfillStats()
    favorites = await load_favorites(db)
    stats.favorites = len(favorites)
    eligible = await _eligible(favorites, stats)
    stats.eligible = len(eligible)
    stats.no_memo = sum(1 for att in eligible if att.memo_id is None)
    ids = [att.id for att in eligible]
    existing = dict(
        (
            await db.execute(
                select(FavoriteFrameJob.attachment_id, FavoriteFrameJob.state).where(
                    FavoriteFrameJob.attachment_id.in_(ids)
                )
            )
        )
        .tuples()
        .all()
    )
    stats.existing_by_state = dict(Counter(str(s) for s in existing.values()))
    stats.would_insert = sum(1 for i in ids if i not in existing)
    stats.would_reset = sum(1 for s in existing.values() if s in RESETTABLE)
    if not execute:
        return stats
    for att in eligible:
        if await _upsert(db, att):
            if att.id in existing:
                stats.reset += 1
            else:
                stats.inserted += 1
    return stats


async def run_backfill(*, execute: bool) -> FavoritesBackfillStats:
    async with async_session() as db:
        stats = await backfill(db, execute=execute)
        if execute:
            await db.commit()
    return stats


async def _amain(*, execute: bool) -> FavoritesBackfillStats:
    if execute and not favorites_configured():
        raise SystemExit(
            "favorites album not configured (FRAME_API_URL / FRAME_API_TOKEN / "
            "FRAME_FAVORITES_ALBUM_ID) — run inside the memos-ng-app container after the album "
            "exists. Refusing to enqueue."
        )
    return await run_backfill(execute=execute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue the hearted pictures for the favorites album."
    )
    parser.add_argument(
        "--execute", action="store_true", help="write the jobs (default: dry run, counts only)"
    )
    args = parser.parse_args()
    stats = asyncio.run(_amain(execute=args.execute))
    print(stats.format(execute=args.execute))


if __name__ == "__main__":
    main()
