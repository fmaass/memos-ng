import asyncio
import json
import os
import subprocess
from collections import deque
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import blurhash_pure

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

        bhash = blurhash_pure.encode(thumb)
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


def poster_seek_seconds(duration: float | None) -> float:
    """Seek past a video's intro before picking the poster — skips black/establishing frames.
    ~12% in, but always leave a window before the end and never seek a very short clip."""
    if not duration or duration <= 2:
        return 0.0
    return max(0.5, min(duration * 0.12, duration - 1.0))


def poster_ffmpeg_cmd(src: str, dest: str, duration: float | None) -> list[str]:
    """Pick a representative 'hero' frame: seek past the intro, then ffmpeg's `thumbnail`
    filter selects the frame most different from its neighbours' average over a 100-frame
    window — the industry-standard cheap heuristic, instead of the odd first frame."""
    return [
        "ffmpeg",
        "-y",
        "-ss",
        f"{poster_seek_seconds(duration):.2f}",
        "-i",
        src,
        "-vf",
        "thumbnail=100",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        dest,
    ]


def process_video_poster(attachment: Attachment) -> dict:
    """Fast lane for video: probe dims/duration + extract a poster frame + blurhash.
    The H.264 transcode (slow lane) lands in T2.3."""
    src = attachment.storage_path
    assert src is not None  # set at finalize
    meta = _ffprobe(src)
    poster_path = _assets("web") / f"{attachment.uid}.poster.jpg"
    poster_path.parent.mkdir(parents=True, exist_ok=True)
    # keep the real extension so ffmpeg can infer the muxer (a .jpg.tmp suffix fails)
    tmp = poster_path.with_name(f"{poster_path.stem}.tmp{poster_path.suffix}")
    subprocess.run(
        poster_ffmpeg_cmd(src, str(tmp), meta.get("duration")),
        capture_output=True,
        timeout=60,
        check=True,
    )
    os.replace(tmp, poster_path)
    _stamp_mtime(poster_path, attachment)
    with Image.open(poster_path) as poster:
        meta["blurhash"] = blurhash_pure.encode(poster)
    return meta


class ThermalDefer(Exception):
    """Raised to re-queue a slow job when the host is too hot (not a failure)."""


def read_temp_millideg() -> int | None:
    try:
        return int(Path(settings.THERMAL_ZONE).read_text().strip())
    except (OSError, ValueError):
        return None  # no thermal zone (local/CI) → guard is a no-op


def too_hot() -> bool:
    temp = read_temp_millideg()
    return temp is not None and temp > settings.THERMAL_MAX_C * 1000


def parse_progress(line: str, duration: float | None) -> int | None:
    """Map an ffmpeg `-progress` line to a 0-100 percent (needs the clip duration)."""
    if not duration or duration <= 0:
        return None
    key, _, value = line.strip().partition("=")
    if key == "out_time_us" and value.isdigit():
        return max(0, min(100, int(int(value) / 1_000_000 / duration * 100)))
    return None


async def process_slow(db: AsyncSession, job: MediaJob, attachment: Attachment) -> None:
    """H.264/AAC transcode (the slow lane). Defers while the host is too hot (ADR thermal)."""
    if too_hot():
        raise ThermalDefer
    src = attachment.storage_path
    assert src is not None
    duration = attachment.duration or _ffprobe(src).get("duration")

    dest = _assets("web") / f"{attachment.uid}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # keep the real extension so ffmpeg infers the mp4 muxer (a .mp4.tmp suffix fails)
    tmp = dest.with_name(f"{dest.stem}.tmp{dest.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-threads",
        str(settings.FFMPEG_THREADS),
        "-vf",
        "scale=-2:min(1080\\,ih)",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(tmp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    assert proc.stdout is not None
    tail: deque[str] = deque(maxlen=12)
    last = 0.0
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore")
            tail.append(line.rstrip())
            pct = parse_progress(line, duration)
            if pct is not None and (asyncio.get_event_loop().time() - last) > 2:
                job.progress_pct = pct
                await db.flush()
                last = asyncio.get_event_loop().time()
        await proc.wait()
    finally:
        # On cancellation (worker shutdown) or any error, don't leak the ffmpeg process or a
        # half-written tmp file — kill it and clean up.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
            tmp.unlink(missing_ok=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)[-300:]}")

    os.replace(tmp, dest)  # atomic; poster was written by the fast lane first
    _stamp_mtime(dest, attachment)
    job.progress_pct = 100
    attachment.status = AttachmentStatus.ready
    await db.flush()


async def process_job(db: AsyncSession, job: MediaJob) -> None:
    """Dispatch a media job to its lane. Raises on failure (caller marks it failed);
    raises ThermalDefer to re-queue without counting a failure."""
    attachment = (
        await db.execute(select(Attachment).where(Attachment.id == job.attachment_id))
    ).scalar_one()

    if job.lane is JobLane.slow:
        if attachment.mime.startswith("video/"):
            await process_slow(db, job, attachment)
        else:
            # Heavy image (HEIC/AVIF): the CPU-costly libheif decode + derivative generation runs
            # on the slow (Merkur) worker. process_image never touches the original (ADR-0008).
            meta = process_image(attachment)
            attachment.width = meta.get("width")
            attachment.height = meta.get("height")
            attachment.blurhash = meta.get("blurhash")
            attachment.status = AttachmentStatus.ready
            await db.flush()
        return

    is_video = attachment.mime.startswith("video/")
    meta = process_video_poster(attachment) if is_video else process_image(attachment)

    attachment.width = meta.get("width")
    attachment.height = meta.get("height")
    attachment.duration = meta.get("duration")
    attachment.blurhash = meta.get("blurhash")
    # Image is complete after the fast lane; video still needs the slow-lane transcode.
    attachment.status = AttachmentStatus.processing if is_video else AttachmentStatus.ready
    if is_video:
        # Enqueue the transcode only now that the fast lane (poster, dims, duration) is done,
        # which guarantees fast→slow ordering regardless of how many workers run.
        db.add(MediaJob(attachment_id=attachment.id, lane=JobLane.slow, state=JobState.queued))
    await db.flush()
