"""Aura frame share — enqueue side (Phase 1).

`maybe_enqueue_frame_shares` is the single choke point both memo router paths (create + edit)
call after attachment linking and before commit. It inserts one `frame_share_jobs` row per linked
image attachment when the feature is fully configured and the memo is #curated. The worker (Phase 2)
drains the queue, renders each photo, and emails it to the frame.
"""

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, FrameShareJob, FrameShareState, Memo, Tag, User, memo_tags

logger = structlog.get_logger(__name__)

# The tag that opts a memo's photos into frame sharing.
CURATED_TAG = "curated"


def is_frame_image(mime: str) -> bool:
    """True for raster image MIMEs the worker can decode + render to JPEG. Videos and other
    attachment types are never shared to the frame (Aura's email path rejects them). SVG is
    excluded — it is not a raster photo and Pillow cannot decode it."""
    m = (mime or "").lower()
    return m.startswith("image/") and m != "image/svg+xml"


def _server_smtp_configured() -> bool:
    """Global on/off switch: empty username or password disables the feature entirely."""
    return bool(settings.SMTP_USERNAME and settings.SMTP_PASSWORD)


async def maybe_enqueue_frame_shares(db: AsyncSession, memo: Memo, user: User) -> int:
    """Enqueue frame-share jobs for a memo's image attachments, if all guards pass. Returns the
    number of NEW rows inserted (0 when any guard fails or every attachment is already queued).

    Guards (all required): server SMTP configured, owner has both Aura settings, memo is #curated.
    Idempotent per attachment via ON CONFLICT (attachment_id) DO NOTHING — so re-editing a curated
    memo never resends existing photos, while a newly added image on an already-curated memo is
    enqueued. Runs inside the caller's transaction (atomic with the memo write)."""
    if not _server_smtp_configured():
        return 0
    if not (user.frame_send_to and user.frame_send_from):
        return 0

    has_curated = (
        await db.execute(
            select(Tag.id)
            .join(memo_tags, memo_tags.c.tag_id == Tag.id)
            .where(memo_tags.c.memo_id == memo.id, Tag.name == CURATED_TAG)
        )
    ).first() is not None
    if not has_curated:
        return 0

    attachments = (
        (await db.execute(select(Attachment).where(Attachment.memo_id == memo.id))).scalars().all()
    )
    inserted = 0
    for att in attachments:
        if not is_frame_image(att.mime):
            continue
        result = await db.execute(
            # Core insert (needed for ON CONFLICT) does NOT apply the ORM column defaults, so set
            # the NOT-NULL state/attempts explicitly. created_at uses the DB server_default.
            # RETURNING id yields a row on insert and nothing on conflict → a typed insert counter.
            pg_insert(FrameShareJob)
            .values(
                attachment_id=att.id,
                memo_id=memo.id,
                user_id=user.id,
                state=FrameShareState.queued,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["attachment_id"])
            .returning(FrameShareJob.id)
        )
        if result.first() is not None:
            inserted += 1

    if inserted:
        logger.info("frame_share.enqueued", memo_id=memo.id, user_id=user.id, jobs=inserted)
    return inserted
