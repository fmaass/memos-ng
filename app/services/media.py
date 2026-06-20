import json
import os
import subprocess
from pathlib import Path

import blurhash
import pillow_heif
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, AttachmentStatus, JobLane, MediaJob

pillow_heif.register_heif_opener()  # lets PIL open HEIC/HEIF camera originals

THUMB_MAX = 800
MOBILE_MAX = 2048


def _assets(sub: str) -> Path:
    return Path(settings.DATA_ROOT) / "assets" / sub


def _atomic_save(img: Image.Image, dest: Path, *, quality: int = 80) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    img.save(tmp, "JPEG", quality=quality)
    os.replace(tmp, dest)


def _stamp_mtime(path: Path, attachment: Attachment) -> None:
    if attachment.captured_at:
        ts = attachment.captured_at.timestamp()
        os.utime(path, (ts, ts))


def process_image(attachment: Attachment) -> dict:
    """Derive thumb + mobile tiers + blurhash + dims via Pillow (+pillow-heif for HEIC).

    pyvips would be faster but needs a system libvips; the bundled-libheif wheels are
    portable. The original is never modified (ADR-0008).
    """
    src = attachment.storage_path
    assert src is not None  # set at finalize
    with Image.open(src) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
        width, height = img.size

        thumb = img.copy()
        thumb.thumbnail((THUMB_MAX, THUMB_MAX))
        thumb_path = _assets("thumbs") / f"{attachment.uid}.jpg"
        _atomic_save(thumb, thumb_path)
        _stamp_mtime(thumb_path, attachment)

        mobile = img.copy()
        mobile.thumbnail((MOBILE_MAX, MOBILE_MAX))
        mobile_path = _assets("mobile") / f"{attachment.uid}.jpg"
        _atomic_save(mobile, mobile_path)
        _stamp_mtime(mobile_path, attachment)

    bhash = blurhash.encode(str(thumb_path), x_components=4, y_components=3)
    return {"width": width, "height": height, "blurhash": bhash}


def _ffprobe(path: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    duration = data.get("format", {}).get("duration")
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": float(duration) if duration else None,
    }


def process_video_poster(attachment: Attachment) -> dict:
    """Fast lane for video: probe dims/duration + extract a poster frame + blurhash.
    The H.264 transcode (slow lane) lands in T2.3."""
    src = attachment.storage_path
    assert src is not None  # set at finalize
    meta = _ffprobe(src)
    poster_path = _assets("web") / f"{attachment.uid}.poster.jpg"
    poster_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = poster_path.with_suffix(".jpg.tmp")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "0.5",
            "-i",
            src,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(tmp),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    os.replace(tmp, poster_path)
    _stamp_mtime(poster_path, attachment)
    meta["blurhash"] = blurhash.encode(str(poster_path), x_components=4, y_components=3)
    return meta


async def process_job(db: AsyncSession, job: MediaJob) -> None:
    """Run a fast-lane job: derive tiers, fill metadata, advance status.
    Raises on failure (caller marks the job failed)."""
    attachment = (
        await db.execute(select(Attachment).where(Attachment.id == job.attachment_id))
    ).scalar_one()
    if job.lane is not JobLane.fast:
        raise RuntimeError("slow lane handled in T2.3")

    is_video = attachment.mime.startswith("video/")
    meta = process_video_poster(attachment) if is_video else process_image(attachment)

    attachment.width = meta.get("width")
    attachment.height = meta.get("height")
    attachment.duration = meta.get("duration")
    attachment.blurhash = meta.get("blurhash")
    # Image is complete after the fast lane; video still needs the slow-lane transcode (T2.3).
    attachment.status = AttachmentStatus.processing if is_video else AttachmentStatus.ready
    await db.flush()
