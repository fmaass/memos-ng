import hashlib
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import puremagic
from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob, Upload


def _data_root() -> Path:
    return Path(settings.DATA_ROOT)


# Blocking disk I/O runs off the event loop (anyio.to_thread) so large chunk writes /
# sha256 reads don't stall the server.
def _append_sync(path: Path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def _sha256_sync(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def part_path(upload_uid: str) -> Path:
    return _data_root() / "uploads" / f"{upload_uid}.part"


def _sniff_mime(path: Path, declared: str) -> str:
    try:
        guessed = puremagic.from_file(str(path), mime=True)
        return guessed or declared
    except (puremagic.PureError, ValueError, OSError):
        return declared


async def create_upload(
    db: AsyncSession, *, filename: str, mime: str, size: int, last_modified: int | None = None
) -> Upload:
    upload_uid = secrets.token_urlsafe(24)
    part = part_path(upload_uid)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.touch()
    client_mtime = datetime.fromtimestamp(last_modified / 1000, UTC) if last_modified else None
    upload = Upload(
        upload_uid=upload_uid,
        filename=filename,
        mime=mime,
        declared_size=size,
        bytes_received=0,
        client_mtime=client_mtime,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.UPLOAD_EXPIRY_HOURS),
    )
    db.add(upload)
    await db.flush()
    return upload


async def append_chunk(db: AsyncSession, upload: Upload, offset: int, data: bytes) -> Upload:
    """Append at `offset`. Idempotent: re-sending an already-received range is a no-op;
    a forward gap is rejected (caller → 409)."""
    if offset == upload.bytes_received:
        await to_thread.run_sync(_append_sync, part_path(upload.upload_uid), data)
        upload.bytes_received += len(data)
        await db.flush()
    elif offset > upload.bytes_received:
        raise ValueError("offset gap")
    # offset < bytes_received → already have it; no-op
    return upload


async def finalize_pending(
    db: AsyncSession, upload_uids: list[str], owner_id: int, moved_out: list[str] | None = None
) -> list[str]:
    """Finalize-on-post: turn each fully-received pending upload into the owner's attachment and
    return their uids. A missing/expired or incomplete upload is skipped (it expires via
    reap_expired) so an abandoned compose never creates an orphan attachment.

    finalize() moves the part file to its final originals/ path BEFORE the (now shared) memo
    transaction commits — a non-transactional side effect. Each moved path is appended to
    `moved_out` so the caller can unlink them if the post later rolls back (else they leak)."""
    out: list[str] = []
    for up_uid in upload_uids:
        upload = (
            await db.execute(select(Upload).where(Upload.upload_uid == up_uid))
        ).scalar_one_or_none()
        if upload is None:
            continue
        part = part_path(up_uid)
        if not part.exists() or part.stat().st_size != upload.declared_size:
            continue  # incomplete — leave it to expire, don't half-finalize
        att = await finalize(db, upload, owner_id=owner_id)
        out.append(att.uid)
        if moved_out is not None and att.storage_path:
            moved_out.append(att.storage_path)
    return out


async def finalize(db: AsyncSession, upload: Upload, owner_id: int) -> Attachment:
    part = part_path(upload.upload_uid)
    actual = part.stat().st_size
    if actual != upload.declared_size:
        raise ValueError(f"size mismatch: {actual} != {upload.declared_size}")

    digest = await to_thread.run_sync(_sha256_sync, part)

    # sha256 dedup — identical bytes already stored BY THIS OWNER → return the existing
    # attachment. Scoped per-owner: a global dedup would hand one user a reference to another
    # user's attachment row (cross-user linkage), so dedup only within the finalizing owner.
    existing = (
        await db.execute(
            select(Attachment).where(
                Attachment.sha256 == digest, Attachment.owner_id == owner_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        part.unlink(missing_ok=True)
        await db.delete(upload)
        await db.flush()
        return existing

    mime = _sniff_mime(part, upload.mime)
    now = datetime.now(UTC)
    dest_dir = _data_root() / "originals" / f"{now:%Y}" / f"{now:%m}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}_{Path(upload.filename).name}"
    await to_thread.run_sync(os.replace, part, dest)

    is_video = mime.startswith("video/")
    attachment = Attachment(
        uid=secrets.token_urlsafe(16),
        owner_id=owner_id,
        filename=upload.filename,
        mime=mime,
        size=actual,
        sha256=digest,
        captured_at=upload.client_mtime or now,
        status=AttachmentStatus.pending if is_video else AttachmentStatus.processing,
        storage_path=str(dest),
        # width/height/duration are filled by the worker fast lane (T2.2 pyvips/ffprobe).
    )
    db.add(attachment)
    await db.flush()

    # Only the fast lane is enqueued up front. For video, the fast lane enqueues the slow
    # (transcode) job once it has produced the poster/dims — so the two can never race a
    # status overwrite under concurrent workers (a slow job setting 'ready' before the fast
    # job sets 'processing' would strand finished media permanently non-ready).
    db.add(MediaJob(attachment_id=attachment.id, lane=JobLane.fast, state=JobState.queued))
    await db.delete(upload)
    await db.flush()
    return attachment


async def reap_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete abandoned uploads (past expires_at) + part files. Worker-wired in T2.2."""
    now = now or datetime.now(UTC)
    rows = (await db.execute(select(Upload).where(Upload.expires_at < now))).scalars().all()
    for upload in rows:
        part_path(upload.upload_uid).unlink(missing_ok=True)
        await db.delete(upload)
    await db.flush()
    return len(rows)
