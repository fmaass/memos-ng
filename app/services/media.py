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
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    JobLane,
    JobState,
    MediaJob,
    MediaJobProgress,
)
from app.services import blurhash_pure

pillow_heif.register_heif_opener()  # lets PIL open HEIC/HEIF camera originals

logger = structlog.get_logger(__name__)

THUMB_MAX = 800
MOBILE_MAX = 2048
# Grace between SIGTERM and SIGKILL when stopping a runaway transcode: ffmpeg closes the output
# file and exits on TERM, so this only has to cover finishing the frame in flight.
KILL_GRACE_SECONDS = 5.0
GROUP_POLL_SECONDS = 0.25  # cadence of the "is anything left in the group?" probe
# Bounded cadence for committed progress samples (see _pump_progress): ffmpeg reports per frame,
# this publishes at most once per interval, so a long transcode costs a handful of tiny commits.
PROGRESS_PUBLISH_SECONDS = 2.0


def _assets(sub: str) -> Path:
    return Path(settings.DATA_ROOT) / "assets" / sub


class AttachmentGone(Exception):
    """Raised when the attachment a job derives from was deleted while the job was running.

    Not a failure of the job: the work is simply moot (MEMNG-41). `delete_attachment` never waits
    for a running job, so the row can vanish under one at any moment; the worker discards the job
    instead of counting an attempt against an attachment that no longer exists.
    """


def derived_outputs(uid: str) -> list[Path]:
    """Every file a media job writes for one attachment — final tiers AND their in-flight temps.

    Used to sweep a job's output when its attachment turns out to be deleted. The temps matter as
    much as the finals: an abandoned `.tmp.mp4` of a 4K clip is the biggest file the worker ever
    holds, and nothing else reaps it.
    """
    web = _assets("web")
    return [
        _assets("thumbs") / f"{uid}.jpg",
        _assets("thumbs") / f"{uid}.jpg.tmp",  # _atomic_save
        _assets("mobile") / f"{uid}.jpg",
        _assets("mobile") / f"{uid}.jpg.tmp",
        web / f"{uid}.poster.jpg",
        web / f"{uid}.poster.tmp.jpg",  # process_video_poster
        web / f"{uid}.mp4",
        web / f"{uid}.tmp.mp4",  # process_slow
    ]


def discard_outputs(uid: str) -> None:
    for path in derived_outputs(uid):
        try:
            path.unlink(missing_ok=True)
        except OSError:  # best-effort sweep; the attachment row is already gone
            pass


async def attachment_still_exists(db: AsyncSession, attachment_id: int) -> bool:
    """Re-read the attachment row UNDER A ROW LOCK, at the point where a job publishes its result.

    This is the serialization point with `delete_attachment`, which no longer waits for a running
    job (MEMNG-41). Without it, a transcode finishing a moment after a delete would `os.replace`
    its output into place and resurrect a file the delete had already swept. With it, only two
    orders are possible, both clean:

    * this job wins — the delete blocks on this lock for the publish alone (milliseconds, until
      the job's commit), then unlinks the just-published tier along with the rest;
    * the delete wins — the row is already gone, this returns False, and the caller discards its
      output instead of publishing it.

    The lock is taken here and released by the job's commit; it is NEVER held across the transcode.
    """
    return (
        await db.execute(
            select(Attachment.id).where(Attachment.id == attachment_id).with_for_update()
        )
    ).scalar_one_or_none() is not None


async def guard_publish(db: AsyncSession, attachment: Attachment) -> None:
    """Checkpoint a job must pass before writing its result. Raises AttachmentGone (after sweeping
    the files this run produced) when the attachment was deleted mid-job — which also keeps the
    job from flushing an UPDATE against a row that no longer exists (0 rows → StaleDataError,
    a spurious 'failure' for work nobody wants any more)."""
    if await attachment_still_exists(db, attachment.id):
        return
    discard_outputs(attachment.uid)
    raise AttachmentGone(f"attachment {attachment.id} was deleted while its job was running")


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


QUARTER_TURNS = (0, 90, 180, 270)


def _as_quarter_turn(value: object) -> int | None:
    """One raw rotation value → a normalized quarter turn, or None when it is not one.

    Both rotation sources are loose about type and sign: the display matrix's angle is a number
    ffprobe derives with atan2, so it arrives negative or float (-90 means "90° clockwise", i.e.
    270° counter-clockwise), while the legacy stream tag is a string. Anything that does not
    normalize to a quarter turn — a shear or arbitrary-angle matrix — is rejected rather than
    rounded to the nearest one: no width/height swap can express such a transform, so the coded
    dimensions stay the honest answer for it.
    """
    if not isinstance(value, int | float | str):
        return None
    try:
        # BOTH exceptions are reachable from a malformed probe, and neither is hypothetical:
        # float() accepts "inf"/"nan" happily, and round() then raises — OverflowError on an
        # infinity, ValueError on a NaN. Uncaught, one junk rotation field would take down the
        # whole probe (no dims, no duration) for a file that is otherwise perfectly playable.
        degrees = round(float(value)) % 360
    except (OverflowError, ValueError):
        return None
    return degrees if degrees in QUARTER_TURNS else None


def probe_rotation(stream: dict) -> int:
    """The rotation ffprobe reports for a video stream, normalized to one of QUARTER_TURNS.

    Where the rotation lives depends on the container/encoder era, so BOTH places are read: a
    Display Matrix in the stream's side data (what phone cameras write today and what the mov/mp4
    demuxer exports) and the legacy `rotate` stream tag (older muxers). The matrix wins whenever
    it is present — that is ffmpeg's own precedence, and matching it is what keeps the dimensions
    reported here in agreement with the frames the transcode actually produces.
    """
    for entry in stream.get("side_data_list") or []:
        if isinstance(entry, dict) and "rotation" in entry:
            # Authoritative once present, including when it is NOT a quarter turn (→ no swap):
            # falling through to the legacy tag here would let a stale tag override the matrix.
            return _as_quarter_turn(entry["rotation"]) or 0
    return _as_quarter_turn((stream.get("tags") or {}).get("rotate")) or 0


def displayed_size(stream: dict) -> tuple[int | None, int | None]:
    """The stream's DISPLAYED width/height — its coded dimensions with the rotation applied.

    A portrait phone clip is stored landscape plus a 90°/270° rotation, so the coded dimensions
    describe the wrong shape (MEMNG-40 / #37) and every consumer of attachment.width/height —
    tile aspect ratio, feed layout — is misled by them.

    Consistency with the derived tiers: ffmpeg AUTOROTATES on decode (verified against the runtime
    image's ffprobe/ffmpeg 7.1.5 — a 320x240 + rotation-90 source transcodes to a 240x320 mp4 with
    no rotation side data, and its poster frame comes out 240x320 too). Both the web tier and the
    poster are therefore already display-oriented, so swapping here AGREES with them rather than
    double-applying: the swap only re-states, for the original, the orientation the derived
    artifacts were written in.
    """
    width, height = stream.get("width"), stream.get("height")
    if probe_rotation(stream) in (90, 270):
        return height, width
    return width, height


VIDEO_STREAM_MAP = "0:V:0"  # the ONE stream the probe measures and both ffmpeg lanes render


def select_video_stream(streams: list) -> dict:
    """The video stream the derived tiers are actually made from — ffmpeg's `0:V:0`.

    This has to agree with the ffmpeg commands or the whole probe describes the wrong picture.
    ffmpeg's DEFAULT stream selection (no -map) picks the highest-RESOLUTION video stream, which
    is not the first one: on a two-stream container a `v:0` probe measured 320x240 while the
    transcode produced 640x480 (reproduced in the runtime image). So both lanes now pass an
    explicit `-map 0:V:0` and this picks the same stream by the same rule.

    `V` (as opposed to `v`) is ffmpeg's specifier for video streams that are NOT attached
    pictures — the cover art in a music file is a 1-frame video stream, and measuring the album
    art instead of the movie is the same bug in a different disguise. A stream with no
    `disposition` at all counts as not-attached, which is how ffmpeg reads a missing flag too.

    Returns {} when nothing qualifies. That is deliberately the same answer ffmpeg gives: with no
    non-attached-pic video stream, `-map 0:V:0` fails the command outright ("Stream map '' matches
    no streams", exit 234), so reporting no dimensions keeps the two consistent — better than
    inventing dimensions from cover art that no derived tier will ever be rendered from.
    """
    for stream in streams:
        if isinstance(stream, dict) and not (stream.get("disposition") or {}).get("attached_pic"):
            return stream
    return {}


def probe_cmd(path: str) -> list[str]:
    """The ffprobe argv (sibling of poster_ffmpeg_cmd / slow_ffmpeg_cmd — a builder so the two
    choices encoded here stay unit-testable).

    `-show_streams`, rather than a narrower `-show_entries stream=width,height`, because the
    rotation has to come back too and the entry filter cannot reach into the side-data list:
    `stream=...,side_data_list` yields entries stripped to `{}` (verified on ffprobe 7.1.5).
    Only the child-section form `stream_side_data=rotation` selects it, and depending on that
    section name would fail SILENTLY — as a rotation that reads absent, i.e. straight back into
    this bug — if it were ever renamed. -show_streams carries both sources unconditionally.

    `-select_streams v` asks for EVERY video stream, not `v:0`: picking the right one is
    select_video_stream's job, because the attached-picture exclusion cannot be expressed as an
    index and `v:0` would hand back the cover art of a music file.
    """
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]


def _ffprobe(path: str) -> dict:
    out = subprocess.run(
        probe_cmd(path),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    data = json.loads(out)
    stream = select_video_stream(data.get("streams") or [])
    duration = data.get("format", {}).get("duration")
    width, height = displayed_size(stream)
    return {
        "width": width,
        "height": height,
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
    window — the industry-standard cheap heuristic, instead of the odd first frame.

    `-map 0:V:0` pins the poster to the SAME stream the probe measured (see
    select_video_stream); without it ffmpeg auto-selects by resolution and a multi-stream
    container gets a poster whose shape contradicts the stored dimensions."""
    return [
        "ffmpeg",
        "-y",
        "-ss",
        f"{poster_seek_seconds(duration):.2f}",
        "-i",
        src,
        "-map",
        VIDEO_STREAM_MAP,
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

    The two -map arguments are a pair and neither is optional:

    * `-map 0:V:0` pins the transcode to the stream the probe measured (see select_video_stream).
      ffmpeg's default selection picks the highest-RESOLUTION video stream instead, so on a
      multi-stream container the web tier and the stored dimensions described different pictures.
    * `-map 0:a:0?` only puts back what the default selection was already doing. Naming ANY -map
      switches ffmpeg out of automatic selection entirely, so the video map alone would silently
      drop the audio track from every transcoded clip. The trailing `?` is load-bearing too: the
      map is otherwise fatal on a source with no audio ("Stream map '' matches no streams",
      exit 234) — which is most phone screen recordings, and every fixture in this test suite.
    """
    return [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-map",
        VIDEO_STREAM_MAP,
        "-map",
        "0:a:0?",
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
    """True while a LIVE (non-zombie) process remains in the transcode's group.

    Group liveness — not the leader's exit status — is what decides whether the tree is really
    gone: ffmpeg's descendants outlive it, keep the stdout pipe open and keep writing the temp
    file.

    Deliberately NOT `killpg(pgid, 0)`: that counts zombies as members. In the container the worker
    process is PID 1, so a grandchild orphaned by the kill is re-parented to the worker ITSELF and
    stays a zombie — a killpg probe would then read the group as alive through both grace periods,
    log transcode_group_unreapable on every timed-out job, and never settle. A zombie cannot react
    to a signal and holds no file or pipe, so for escalation purposes it is GONE.

    Reaping those zombies is the CONTAINER INIT's job (`init: true` on the worker service), not
    this code's: an os.waitpid(-1, ...) sweep here would race asyncio's child watcher for the exit
    statuses of the children it owns.
    """
    try:
        pids = os.listdir("/proc")
    except OSError:  # no procfs (non-Linux dev box): nothing enumerable, signals were still sent
        return False
    for name in pids:
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_bytes()
        except OSError:
            continue  # exited between the listing and the read — by definition not alive
        # `pid (comm) state ppid pgrp ...` — comm is arbitrary BYTES (not UTF-8; decoding can
        # raise on someone else's process name) and can contain spaces and parentheses, so the
        # line stays bytes and fields are read after the LAST ')'.
        fields = stat.rpartition(b")")[2].split()
        if len(fields) >= 3 and fields[2] == str(pgid).encode() and fields[0] != b"Z":
            return True
    return False


async def _await_group_exit(pgid: int, proc: asyncio.subprocess.Process) -> None:
    """Wait until no live member remains in the group AND the leader has been reaped.

    Unbounded by design — the caller wraps it in asyncio.timeout, which is the only bound that
    matters here. The leader is waited on explicitly (rather than left to the group probe, which
    reads a zombie as gone) so this process reaps the one child it actually owns.
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
    """Stop the transcode *and everything it spawned*, and reap the one child this process owns.

    Descendants orphaned by the kill are reaped by the container init (`init: true`), not here —
    see _group_alive for why this code must not run a waitpid sweep of its own.

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


async def publish_progress(job_id: int, pct: int) -> None:
    """Commit one progress sample for a running job, in a session of its OWN (MEMNG-36).

    Why not the job row: the worker holds SELECT … FOR UPDATE on it for the whole transcode, so a
    percentage written there is invisible to every other session until the job's final commit —
    which is exactly when it stops being interesting — and a second session trying to write it
    would block on the row lock for the length of the transcode. This session touches only
    media_job_progress, is opened and committed per sample, and holds nothing.

    Best-effort by construction: a progress sample is telemetry, and losing one (or every one)
    must never fail a transcode that is otherwise fine. Every error is logged and swallowed,
    including a failure to even open the session. This runs OUTSIDE the job's transaction, so a
    poisoned session here cannot poison that one.
    """
    try:
        async with async_session() as db:
            await db.execute(
                pg_insert(MediaJobProgress)
                .values(job_id=job_id, progress_pct=pct)
                .on_conflict_do_update(
                    index_elements=[MediaJobProgress.job_id],
                    set_={"progress_pct": pct, "updated_at": func.now()},
                )
            )
            await db.commit()
    except Exception as exc:  # telemetry must never break the work it describes
        logger.warning("job_progress_publish_failed", job_id=job_id, error=str(exc))


async def clear_progress(job_id: int) -> None:
    """Drop a job's progress row once it stops running, in its own session and best-effort.

    Called whichever way the transcode ended (success, ffmpeg error, timeout, a delete that
    landed mid-job), so the side channel holds only jobs that are actually running. The worker's
    periodic sweep is the backstop for the one case this cannot cover: a process killed outright.
    """
    try:
        async with async_session() as db:
            await db.execute(sa_delete(MediaJobProgress).where(MediaJobProgress.job_id == job_id))
            await db.commit()
    except Exception as exc:
        logger.warning("job_progress_clear_failed", job_id=job_id, error=str(exc))


async def sweep_orphan_progress() -> int:
    """Delete progress rows whose media_jobs row is gone; returns how many were removed.

    clear_progress covers every ordinary ending, so this only ever finds rows left by a worker
    that died mid-transcode on a job later deleted with its attachment. Anti-join rather than an
    age cutoff: a legitimate transcode may run for hours (SLOW_TRANSCODE_TIMEOUT_S), and no
    horizon that survives that is also tight enough to be worth having. Raises — the worker's
    `_safe` wrapper isolates it.
    """
    async with async_session() as db:
        live = select(MediaJob.id).where(MediaJob.id == MediaJobProgress.job_id).exists()
        # RETURNING rather than rowcount: the deleted ids are what the count is counting.
        result = await db.execute(
            sa_delete(MediaJobProgress).where(~live).returning(MediaJobProgress.job_id)
        )
        swept = len(result.all())
        await db.commit()
    return swept


async def _pump_progress(
    job_id: int,
    proc: asyncio.subprocess.Process,
    tail: deque[str],
    duration: float | None,
) -> None:
    """Consume ffmpeg's `-progress` stream (publishing a committed sample at most every
    PROGRESS_PUBLISH_SECONDS), then wait for the process to exit. Split out of process_slow so the
    read loop AND the exit sit under one asyncio.wait_for ceiling — a hang can happen in either.

    The throttle is what keeps this affordable: ffmpeg emits a progress block per frame, and one
    committed round-trip per frame would be a write storm against a Postgres shared with
    production. One sample every couple of seconds is finer than the SSE poll interval anyway.
    """
    assert proc.stdout is not None
    last = 0.0
    async for raw in proc.stdout:
        line = raw.decode(errors="ignore")
        tail.append(line.rstrip())
        pct = parse_progress(line, duration)
        now = asyncio.get_event_loop().time()
        if pct is not None and (now - last) > PROGRESS_PUBLISH_SECONDS:
            await publish_progress(job_id, pct)
            last = now
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
    # The pump publishes from a session of its own, so it takes the id — never this instance, which
    # belongs to the claiming transaction.
    job_id = job.id
    tail: deque[str] = deque(maxlen=12)
    ceiling = settings.SLOW_TRANSCODE_TIMEOUT_S
    timed_out = False
    completed = False
    try:
        await asyncio.wait_for(_pump_progress(job_id, proc, tail, duration), ceiling)
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
        # The transcode is over either way, so the side channel's row for it is stale from here on
        # — including on the failure paths, where the job goes back to `queued` and would otherwise
        # keep advertising the percentage the dead attempt reached.
        await clear_progress(job_id)
    if timed_out:
        raise RuntimeError(f"slow-lane transcode timed out after {ceiling}s")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)[-300:]}")

    # The delete endpoint does not wait for this job, so the attachment may have gone away during
    # the transcode. Check under the row lock BEFORE publishing: past this point the tmp file
    # becomes the served web tier, and republishing it after a delete would resurrect it.
    await guard_publish(db, attachment)  # sweeps tmp + any tier files when the delete won
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
    ).scalar_one_or_none()
    if attachment is None:
        # Deleted before this job got its turn: delete_attachment removes every job row it can
        # lock, but not one a worker already holds — and a claim that rolls back (restart, failed
        # outcome write) returns that row to `queued` for a later poll (MEMNG-41).
        raise AttachmentGone(f"attachment {job.attachment_id} no longer exists")

    if job.lane is JobLane.slow:
        if attachment.mime.startswith("video/"):
            await process_slow(db, job, attachment)
        else:
            # Heavy image (HEIC/AVIF): the CPU-costly libheif decode + derivative generation runs
            # on the slow (Merkur) worker. process_image never touches the original (ADR-0008).
            meta = process_image(attachment)
            await guard_publish(db, attachment)
            attachment.width = meta.get("width")
            attachment.height = meta.get("height")
            attachment.blurhash = meta.get("blurhash")
            attachment.status = AttachmentStatus.ready
            await db.flush()
        return

    is_video = attachment.mime.startswith("video/")
    meta = process_video_poster(attachment) if is_video else process_image(attachment)

    await guard_publish(db, attachment)
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
