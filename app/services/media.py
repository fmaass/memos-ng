import asyncio
import json
import os
import signal
import subprocess
from collections import deque
from pathlib import Path

import pillow_heif
import structlog
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import blurhash_pure

pillow_heif.register_heif_opener()  # lets PIL open HEIC/HEIF camera originals

logger = structlog.get_logger(__name__)

THUMB_MAX = 800
MOBILE_MAX = 2048
# Grace between SIGTERM and SIGKILL when stopping a runaway transcode: ffmpeg closes the output
# file and exits on TERM, so this only has to cover finishing the frame in flight.
KILL_GRACE_SECONDS = 5.0
GROUP_POLL_SECONDS = 0.25  # cadence of the "is anything left in the group?" probe


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


def slow_ffmpeg_cmd(src: str, dest: str, threads: int) -> list[str]:
    """The slow-lane H.264/AAC transcode command (sibling of poster_ffmpeg_cmd for the fast lane).

    Built separately from process_slow so the argv is unit-testable and substitutable — the
    timeout tests drive the real subprocess machinery with a stub binary instead of relying on
    real ffmpeg wedging.
    """
    return [
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
        str(threads),
        "-vf",
        "scale=-2:min(1080\\,ih)",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        dest,
    ]


def _capture_pgid(proc: asyncio.subprocess.Process) -> int:
    """The transcode's process-group id, read at spawn while the child certainly still exists.

    start_new_session=True makes the child a session/group leader (pgid == pid), but the value is
    read rather than assumed, and read IMMEDIATELY: once the leader exits and is reaped its pid can
    be recycled, and a pgid derived later could then address a stranger's group. No await runs
    between the spawn and this call, so the lookup cannot race a reap; the fallback exists only so
    a cleanup helper can never raise.
    """
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return proc.pid


def _signal_group(pgid: int, sig: int) -> None:
    """Signal the whole transcode group. Never raises — this runs from a `finally`, where an
    exception would replace the primary failure (the timeout) with a cleanup error.

    ESRCH means the group is already empty, which is the outcome being asked for. Any OTHER errno
    is a real problem (we could not deliver the kill) and is logged loudly with the errno rather
    than swallowed — a silent failure here is exactly how an orphan survives unnoticed.
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # nobody left in the group — success, not an error
    except OSError as exc:
        logger.error(
            "transcode_group_signal_failed",
            pgid=pgid,
            signal=sig,
            errno=exc.errno,
            error=str(exc),
        )


def _group_alive(pgid: int) -> bool:
    """True while ANY process remains in the transcode's group (signal 0 = existence probe).

    Group liveness — not the leader's exit status — is what decides whether the tree is really
    gone: ffmpeg's descendants outlive it, keep the stdout pipe open and keep writing the temp
    file. An unexpected errno reads as ALIVE so the caller escalates instead of declaring victory
    on an error it did not expect (it is reported by _signal_group's next call).
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.error("transcode_group_probe_failed", pgid=pgid, errno=exc.errno, error=str(exc))
    return True


async def _await_group_exit(pgid: int, proc: asyncio.subprocess.Process) -> None:
    """Wait until the group is empty AND the leader has been reaped.

    Unbounded by design — the caller wraps it in asyncio.timeout, which is the only bound that
    matters here. The leader is reaped along the way because its own zombie stays a member of the
    group and would otherwise read as a live descendant forever.
    """
    while proc.returncode is None or _group_alive(pgid):
        if proc.returncode is None:
            try:  # reaps the leader the moment it dies; the short bound paces the poll
                await asyncio.wait_for(proc.wait(), GROUP_POLL_SECONDS)
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(GROUP_POLL_SECONDS)


async def _stop_transcode(pgid: int, proc: asyncio.subprocess.Process) -> None:
    """Stop the transcode *and everything it spawned*, then reap it — no zombie, no orphan.

    Everything here is keyed on the GROUP, never on the leader: a leader that already exited does
    NOT prove the tree is gone (a descendant can still hold the pipe and the temp file), and a
    descendant that ignores TERM must still be escalated to SIGKILL after the leader is long dead.
    TERM first (ffmpeg closes its output and exits), SIGKILL after a short grace. Every wait is
    BOUNDED: recovering from a hang must not become a second hang in the worker's only slot.
    """
    if not _group_alive(pgid) and proc.returncode is not None:
        return  # nothing left to signal and the leader is already reaped
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(pgid, sig)
        try:
            async with asyncio.timeout(KILL_GRACE_SECONDS):
                await _await_group_exit(pgid, proc)
            return
        except TimeoutError:
            continue  # something ignored the signal — escalate
    # Survived SIGKILL: an uninterruptible-sleep (D-state) process, e.g. a stalled share read.
    # Nothing more this process can do, but it must be visible — it still holds a worker slot.
    logger.error("transcode_group_unreapable", pgid=pgid, pid=proc.pid)


async def _pump_progress(
    db: AsyncSession,
    job: MediaJob,
    proc: asyncio.subprocess.Process,
    tail: deque[str],
    duration: float | None,
) -> None:
    """Consume ffmpeg's `-progress` stream (flushing job.progress_pct at most every 2s), then wait
    for the process to exit. Split out of process_slow so the read loop AND the exit sit under one
    asyncio.wait_for ceiling — a hang can happen in either."""
    assert proc.stdout is not None
    last = 0.0
    async for raw in proc.stdout:
        line = raw.decode(errors="ignore")
        tail.append(line.rstrip())
        pct = parse_progress(line, duration)
        if pct is not None and (asyncio.get_event_loop().time() - last) > 2:
            job.progress_pct = pct
            await db.flush()
            last = asyncio.get_event_loop().time()
    await proc.wait()


async def process_slow(db: AsyncSession, job: MediaJob, attachment: Attachment) -> None:
    """H.264/AAC transcode (the slow lane). Defers while the host is too hot (ADR thermal).
    Time-bound by SLOW_TRANSCODE_TIMEOUT_S: on expiry the process tree is killed and the job
    fails through the worker's normal failure path (MEMNG-38)."""
    if too_hot():
        raise ThermalDefer
    src = attachment.storage_path
    assert src is not None
    duration = attachment.duration or _ffprobe(src).get("duration")

    dest = _assets("web") / f"{attachment.uid}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # keep the real extension so ffmpeg infers the mp4 muxer (a .mp4.tmp suffix fails)
    tmp = dest.with_name(f"{dest.stem}.tmp{dest.suffix}")
    cmd = slow_ffmpeg_cmd(src, str(tmp), settings.FFMPEG_THREADS)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Own session ⇒ own process group, so a wedged transcode can be signalled as a whole
        # TREE (see _stop_transcode) instead of orphaning whatever ffmpeg forked.
        start_new_session=True,
    )
    pgid = _capture_pgid(proc)  # read at spawn — a later lookup could address a recycled pid
    tail: deque[str] = deque(maxlen=12)
    ceiling = settings.SLOW_TRANSCODE_TIMEOUT_S
    timed_out = False
    completed = False
    try:
        await asyncio.wait_for(_pump_progress(db, job, proc, tail, duration), ceiling)
        completed = proc.returncode == 0
    except TimeoutError:
        # Unbounded, a wedged ffmpeg held the worker's only slot forever and the job never failed
        # visibly (MEMNG-38). Fall through the cleanup below, then fail loudly and namedly.
        timed_out = True
    finally:
        # On the timeout, on cancellation (worker shutdown), on any error — and on success too:
        # the sweep is unconditional because a leader that exited does not prove its descendants
        # did (they can still hold the pipe and write the temp file). It is a cheap no-op when the
        # group is already empty.
        await _stop_transcode(pgid, proc)
        # Keyed on "this run finished cleanly", NOT on the leader's exit status: a leader can exit
        # 0 while a descendant hangs and the ceiling fires, and that partial must still go.
        if not completed:
            tmp.unlink(missing_ok=True)
    if timed_out:
        raise RuntimeError(f"slow-lane transcode timed out after {ceiling}s")
    if proc.returncode != 0:
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
