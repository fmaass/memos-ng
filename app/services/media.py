import asyncio
import json
import os
import signal
import subprocess
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

import pillow_heif
import structlog
from PIL import Image, ImageCms, ImageOps
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    JobKind,
    JobLane,
    JobState,
    MediaJob,
    MediaJobProgress,
)
from app.services import blurhash_pure, poster, printready

pillow_heif.register_heif_opener()  # lets PIL open HEIC/HEIF camera originals

logger = structlog.get_logger(__name__)

THUMB_MAX = 800
MOBILE_MAX = 2048
# The print rendition (MEMNG-49). Every number here is a print requirement, not a screen one, and
# each is measured in docs/plans/PLAN_2026-07-30_photobook-print-tier.md:
#   4096 px  — 1.5 pages of a 21x21 cm layflat book at 300 dpi (3720 px) plus bleed, rounded up;
#   q92      — q85 saves 38% of the bytes and shows on 382 g/m² gloss; q95 costs 37% for nothing;
#   4:4:4    — chroma subsampling artefacts are visible in print, and the saving is ~28% of ~1 GB;
#   300 dpi  — nothing else in this codebase writes a DPI, so layout software assumes 72 and
#              mis-scales the photo on placement. This is the tag that makes the tier usable.
# Re-exported, not redefined: the rung is tier identity and lives in app/services/printready.py,
# which the app process can import without pulling in Pillow (the photo-book manifest projects a
# rendition's size from it). Two literals here and there would be two rungs the day one changes.
PRINTREADY_MAX = printready.MAX_EDGE
PRINTREADY_QUALITY = 92
PRINTREADY_SUBSAMPLING = 0  # 4:4:4 — keep full chroma resolution
PRINTREADY_DPI = (300, 300)
# Grace between SIGTERM and SIGKILL when stopping a runaway transcode: ffmpeg closes the output
# file and exits on TERM, so this only has to cover finishing the frame in flight.
KILL_GRACE_SECONDS = 5.0
GROUP_POLL_SECONDS = 0.25  # cadence of the "is anything left in the group?" probe
# Bounded cadence for committed progress samples (see _pump_progress): ffmpeg reports per frame,
# this publishes at most once per interval, so a long transcode costs a handful of tiny commits.
PROGRESS_PUBLISH_SECONDS = 2.0


# Why this module keeps its filesystem calls inline while the request-serving code does not
# (MEMNG-42). This module is imported by app/worker.py and by nothing else under app/, so none of
# it ever runs on the loop that answers HTTP requests. worker.run() drains one job at a time and
# starts nothing concurrently, so the only task an inline stat/mkdir/unlink here could delay is
# the very job making the call. Offloading would buy no latency for anyone and would put an await
# in places (a cancellation-time `finally`) where it is actively worse — so these sites carry a
# `# blocking-ok:` marker with their individual reason and are pinned in
# tests/test_no_blocking_io.py. If this module ever gains a caller in the app process, that
# premise dies and every one of them has to move off the loop.
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
    print_ready = printready.printready_path(uid)
    poster_file = poster.poster_path(uid)
    return [
        _assets("thumbs") / f"{uid}.jpg",
        _assets("thumbs") / f"{uid}.jpg.tmp",  # _atomic_save
        _assets("mobile") / f"{uid}.jpg",
        _assets("mobile") / f"{uid}.jpg.tmp",
        print_ready,
        print_ready.with_suffix(print_ready.suffix + ".tmp"),  # _atomic_save
        poster_file,  # the hero frame OR a client-supplied override (app/services/poster.py)
        poster.poster_tmp_path(uid),  # in flight: process_video_poster, or an override's copy
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


async def guard_publish(db: AsyncSession, job: MediaJob, attachment: Attachment) -> None:
    """Checkpoint a job must pass before writing its result. Raises AttachmentGone (after sweeping
    the files this run produced) when the attachment was deleted mid-job — which also keeps the
    job from flushing an UPDATE against a row that no longer exists (0 rows → StaleDataError,
    a spurious 'failure' for work nobody wants any more).

    Locks THIS job's media_jobs row BEFORE the attachment row (attachment_still_exists), matching
    the order delete_attachment takes — jobs first (its SKIP-LOCKED cleanup), then the attachment.
    Since MEMNG-90 commits the claim before processing, this job no longer holds the media_jobs lock
    across the transcode, so without a consistent order the worker (attachment→job at its result
    commit) and a concurrent delete (job→attachment) would deadlock. A job row that has already
    vanished means delete_attachment removed it — it won the race — so treat it exactly like a
    deleted attachment: sweep this run's output and discard."""
    locked = (
        await db.execute(select(MediaJob).where(MediaJob.id == job.id).with_for_update())
    ).scalar_one_or_none()
    if locked is not None and await attachment_still_exists(db, attachment.id):
        return
    # blocking-ok: worker-only, and it runs while this job holds the attachment/job row lock — the
    # sweep must finish before the caller can publish anything, so it is not a yield point.
    discard_outputs(attachment.uid)
    raise AttachmentGone(
        f"attachment {attachment.id} (job {job.id}) was deleted while its job was running"
    )


def _atomic_save(img: Image.Image, dest: Path, *, quality: int = 80, **params: Any) -> None:
    """Encode to a sibling temp and rename it into place, so no reader ever sees a partial tier.

    `params` passes the extra JPEG knobs the print rendition needs (subsampling, dpi, icc_profile)
    straight through to Pillow; the screen tiers pass none and keep their encode byte-for-byte.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    img.save(tmp, "JPEG", quality=quality, **params)
    os.replace(tmp, dest)


def stamp_mtime(path: Path, captured_at: datetime | None) -> None:
    """Give a derived file the capture date of the photo or clip it was derived from.

    Takes the VALUE, not the row: a caller that is not the worker (app.scripts.backfill_hdr_posters)
    holds plain columns rather than a live ORM instance, and reading an attribute off an expired
    one is lazy IO in a place that cannot await it. One definition either way — _stamp_mtime is
    the row-shaped spelling of exactly this.
    """
    if captured_at:
        ts = captured_at.timestamp()
        os.utime(path, (ts, ts))


def _stamp_mtime(path: Path, attachment: Attachment) -> None:
    stamp_mtime(path, attachment.captured_at)


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


@lru_cache(maxsize=1)
def srgb_profile() -> ImageCms.ImageCmsProfile:
    """The sRGB profile this deployment converts to and embeds — built once (it is ~600 bytes of
    little-cms state, and every print rendition needs the same one)."""
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def to_srgb(img: Image.Image) -> tuple[Image.Image, bytes]:
    """Colour-MANAGE an image into sRGB and return it with the profile bytes to embed.

    `.convert("RGB")` — what every other tier does — changes the pixel MODE and nothing else: the
    numbers are reinterpreted as sRGB whatever space they were actually in, so an AdobeRGB or
    Display-P3 master (wider gamut, same numbers) prints visibly dull and desaturated. A real
    transform through little-cms is the difference, and embedding the destination profile is the
    other half: the book service requires sRGB and an untagged JPEG is a guess at the far end.

    A source with no embedded profile is assumed to be sRGB — the same assumption every browser
    makes, and the only one available. A source whose profile cannot be read falls back to that
    assumption too, loudly: a corrupt ICC must not cost the photo its print rendition, and the
    numbers are still far closer to right than to wrong.
    """
    profile = srgb_profile()
    icc = img.info.get("icc_profile")
    if img.mode in ("P", "PA"):
        # little-cms cannot transform a palette. The entries are colour values in the source
        # space, so expanding first and transforming after is the same picture, correctly managed.
        img = img.convert("RGB")
    if icc:
        try:
            converted = ImageCms.profileToProfile(
                img, ImageCms.getOpenProfile(BytesIO(icc)), profile, outputMode="RGB"
            )
        except (ImageCms.PyCMSError, OSError) as exc:
            logger.warning("printready_icc_unreadable", error=str(exc))
            converted = None
        if converted is not None:
            return converted, profile.tobytes()
    return img.convert("RGB"), profile.tobytes()


def process_printready(attachment: Attachment) -> Path:
    """Derive the print rendition of one photo and publish it. Returns the file it wrote.

    Source preference is the point of the tier: the PRINT MASTER when the photo has one — that is
    the user's own export for print, crop and output sharpening included — and only otherwise the
    original (app/services/printready.py:source_path). Never a screen tier.

    Downscaled with LANCZOS rather than `thumbnail`'s default bicubic: this is a 60 MP → 4096 px
    reduction whose result is inspected at 300 dpi on gloss paper, where bicubic's softness is the
    one artefact a reader can actually see. `thumbnail` never enlarges, so a source already at or
    below 4096 px keeps its own dimensions — upscaling would invent detail and print worse than
    the honest smaller image.
    """
    source = printready.source_path(attachment)
    if source is None:  # enqueue refuses these; a hand-made job must fail loudly, not silently
        raise RuntimeError(f"attachment {attachment.id} has no source file for a print rendition")
    dest = printready.printready_path(attachment.uid)
    with Image.open(source) as raw:
        img = ImageOps.exif_transpose(raw)  # orientation first — the same path the tiers use
        img, icc = to_srgb(img)
        img.thumbnail((PRINTREADY_MAX, PRINTREADY_MAX), Image.Resampling.LANCZOS)
        _atomic_save(
            img,
            dest,
            quality=PRINTREADY_QUALITY,
            subsampling=PRINTREADY_SUBSAMPLING,
            optimize=True,
            dpi=PRINTREADY_DPI,
            icc_profile=icc,
        )
    _stamp_mtime(dest, attachment)  # carries the capture date, like every other derived tier
    return dest


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
        **stream_colour(stream),
    }


def poster_seek_seconds(duration: float | None) -> float:
    """Seek past a video's intro before picking the poster — skips black/establishing frames.
    ~12% in, but always leave a window before the end and never seek a very short clip."""
    if not duration or duration <= 2:
        return 0.0
    return max(0.5, min(duration * 0.12, duration - 1.0))


# The two transfer curves a JPEG cannot carry, spelled the way ffprobe reports them and zimg
# accepts them (`ffmpeg -h filter=zscale` lists both under `transfer`: arib-std-b67 = 18,
# smpte2084 = 16). HLG is what every iPhone records by default; PQ is the other HDR curve.
HDR_TRANSFERS = frozenset({"arib-std-b67", "smpte2084"})
# What a camera writes alongside either curve when it states the rest of the triplet at all. Used
# only as the fallback for a stream that carries the HDR transfer and nothing else — never to
# DECIDE that a stream is HDR (see is_hdr).
HDR_FALLBACK_PRIMARIES = "bt2020"
HDR_FALLBACK_MATRIX = "bt2020nc"
# HLG's nominal peak luminance in cd/m2, handed to zscale for the linear-light stage. The value
# the reference HLG system is graded against; it is what makes the tone-map's input scale mean
# something rather than being read off an unsignalled default.
HDR_NOMINAL_PEAK_NITS = 100
# The colour tags read off the SELECTED video stream (select_video_stream) — the same stream both
# ffmpeg lanes render. `pix_fmt` is carried for the record and for logs: 10-bit is a strong HDR
# hint but never the decision.
COLOUR_FIELDS = ("color_transfer", "color_primaries", "color_space", "pix_fmt")
# ffprobe SPELLS an unsignalled tag rather than omitting it, and zimg can build no conversion path
# from an unspecified primary or matrix — so these three readings all mean "this stream does not
# say", and the HDR fallback above is used instead.
_UNSTATED_COLOUR = frozenset({"", "unknown", "reserved"})
# The frame heuristic, alone, is the whole SDR filter chain — the command as it has always been.
POSTER_FRAME_FILTER = "thumbnail=100"


def stream_colour(stream: dict) -> dict[str, str | None]:
    """The colour tags of one probed stream, every field present and every value a str or None.

    Always the same four keys, so a caller never has to distinguish "absent" from "unstated": the
    probe's answer for a stream that says nothing about its colour is four Nones, which is exactly
    how an SDR clip and an untagged one have to read.
    """
    colour: dict[str, str | None] = {}
    for field in COLOUR_FIELDS:
        value = stream.get(field)
        colour[field] = value if isinstance(value, str) else None
    return colour


def is_hdr(transfer: str | None) -> bool:
    """Whether a stream's transfer characteristic has to be tone-mapped before it becomes a JPEG.

    Decided by the TRANSFER alone. HLG (`arib-std-b67`) and PQ (`smpte2084`) are the two curves a
    still image cannot carry: JPEG signals no transfer function, so a viewer applies sRGB to
    whatever code words it finds, and HLG/PQ values land far from where they were authored —
    lifted blacks, flat contrast, washed-out colour, while the clip itself plays correctly because
    its container kept the tags. Everything else (`bt709`, `unknown`, missing) is already
    display-referred and is left alone.

    Wide primaries are deliberately NOT part of the test: bt2020 SDR exists, and tone-mapping a
    picture that was never HDR would darken it for nothing.
    """
    return transfer in HDR_TRANSFERS


def _colour_tag(colour: Mapping[str, Any], field: str, fallback: str) -> str:
    value = colour.get(field)
    if isinstance(value, str) and value not in _UNSTATED_COLOUR:
        return value
    return fallback


def poster_video_filter(colour: Mapping[str, Any] | None = None) -> str:
    """The poster's `-vf` chain: the frame heuristic, plus a tone-map when the source is HDR.

    `colour` is the probe's dict (extra keys are ignored, so `_ffprobe`'s whole answer can be
    handed over as it is). SDR, unstated and absent colour all return exactly
    `POSTER_FRAME_FILTER`, so nothing changes for a library of bt709 clips.

    For HLG/PQ the conversion runs AFTER `thumbnail=100`, so it is paid for the ONE frame that
    becomes the poster and not for the hundred it was chosen from:

    * `zscale=tin=..:pin=..:min=..:t=linear:npl=..` decodes the source's own curve, primaries and
      matrix to linear light. The tags come from the probe; a stream that states the HDR transfer
      and nothing else gets the bt2020 pair every HLG/PQ camera writes.
    * `format=gbrpf32le` — `tonemap` reads linear floating-point RGB and refuses anything else.
    * `zscale=p=bt709` moves the gamut to BT.709 first, so the operator compresses in the space
      the JPEG will be displayed in rather than in one the file cannot describe.
    * `tonemap=tonemap=mobius:desat=0` compresses the highlights. Mobius rather than the filmic
      hable: compared frame-by-frame on a real HLG clip from this library, hable pulled the
      midtones ~50/255 below how the scene reads and left the picture perceptibly dim, while
      mobius holds the lower range nearly linear and compresses only against the ceiling —
      0.05% of pixels touched 250+, against hable's none, and blacks stayed put. `desat=0`
      stops the filter from washing bright pixels towards white either way.
    * `zscale=t=bt709:m=bt709:r=tv` writes the BT.709 curve and matrix back at limited range —
      the signalling a JPEG viewer assumes anyway, now true of the pixels as well.
    * `format=yuv420p` is what the JPEG encoder takes.
    """
    colour = colour or {}
    transfer = _colour_tag(colour, "color_transfer", "")
    if not is_hdr(transfer):
        return POSTER_FRAME_FILTER
    primaries = _colour_tag(colour, "color_primaries", HDR_FALLBACK_PRIMARIES)
    matrix = _colour_tag(colour, "color_space", HDR_FALLBACK_MATRIX)
    return (
        f"{POSTER_FRAME_FILTER},"
        f"zscale=tin={transfer}:pin={primaries}:min={matrix}:t=linear:npl={HDR_NOMINAL_PEAK_NITS},"
        "format=gbrpf32le,"
        "zscale=p=bt709,"
        "tonemap=tonemap=mobius:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,"
        "format=yuv420p"
    )


def poster_ffmpeg_cmd(
    src: str, dest: str, duration: float | None, colour: Mapping[str, Any] | None = None
) -> list[str]:
    """Pick a representative 'hero' frame: seek past the intro, then ffmpeg's `thumbnail`
    filter selects the frame most different from its neighbours' average over a 100-frame
    window — the industry-standard cheap heuristic, instead of the odd first frame.

    `-map 0:V:0` pins the poster to the SAME stream the probe measured (see
    select_video_stream); without it ffmpeg auto-selects by resolution and a multi-stream
    container gets a poster whose shape contradicts the stored dimensions.

    `colour` is the probe's reading of that same stream and decides the filter chain alone (see
    poster_video_filter). Omitted, the argv is the one this command has always produced."""
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
        poster_video_filter(colour),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        dest,
    ]


def process_video_poster(attachment: Attachment) -> dict:
    """Fast lane for video: probe dims/duration + extract a poster frame + blurhash.
    The H.264 transcode (slow lane) lands in T2.3.

    A poster ALREADY on disk is a client-supplied override placed with the memo
    (`app/services/uploads.attach_video_poster`) and is never overwritten: the hero-frame heuristic
    deliberately seeks past the intro, which is precisely the frame such a client picked, so
    deriving one here would throw the chosen image away. Only the extraction is skipped — the probe
    still runs (dimensions and duration are properties of the video, not of its poster) and the
    blurhash is still computed from whichever poster is there, so the tile's placeholder always
    matches the image it fades into.

    The probe's answer is handed to the extraction whole, because an HDR source needs its own
    frame tone-mapped before it can be a JPEG (see poster_video_filter). An HDR clip whose poster
    was already on disk therefore keeps its untouched frame — that file is a client's choice, and
    replacing it is app.scripts.backfill_hdr_posters' job, not this lane's.
    """
    src = attachment.storage_path
    assert src is not None  # set at finalize
    meta = _ffprobe(src)
    poster_file = poster.poster_path(attachment.uid)
    if not poster_file.exists():
        poster_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = poster.poster_tmp_path(attachment.uid)
        subprocess.run(
            poster_ffmpeg_cmd(src, str(tmp), meta.get("duration"), colour=meta),
            capture_output=True,
            timeout=60,
            check=True,
        )
        os.replace(tmp, poster_file)
        _stamp_mtime(poster_file, attachment)
    with Image.open(poster_file) as image:
        meta["blurhash"] = blurhash_pure.encode(image)
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
    # blocking-ok: procfs, not the data volume — a /proc listing costs no disk I/O, and this is
    # the poll condition of a bounded kill/reap loop in the worker.
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
    # blocking-ok: procfs again (see _await_group_exit) — a cheap in-memory read, worker-only.
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


async def sweep_stale_progress() -> int:
    """Delete progress rows no running job stands behind; returns how many were removed.

    Stale means both shapes the worker's own cleanup can miss when it is killed outright: the job
    row is GONE (deleted with its attachment), or the job has FINISHED (done/failed) between its
    outcome commit and worker.run_once's post-commit clear. A row whose job is still queued or
    processing is spared — from any other session a running job's row reads `queued`, because its
    claim is uncommitted, so "not terminal" is exactly "may still be running".

    Anti-join rather than an age cutoff: a legitimate transcode may run for hours
    (SLOW_TRANSCODE_TIMEOUT_S), and no horizon that survives that is tight enough to be worth
    having. Raises — the worker's `_safe` wrapper isolates it.
    """
    async with async_session() as db:
        live = (
            select(MediaJob.id)
            .where(
                MediaJob.id == MediaJobProgress.job_id,
                MediaJob.state.notin_((JobState.done, JobState.failed)),
            )
            .exists()
        )
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
    # blocking-ok: a sysfs thermal-zone read (/sys/class/thermal), not the data volume.
    if too_hot():
        raise ThermalDefer
    src = attachment.storage_path
    assert src is not None
    duration = attachment.duration or _ffprobe(src).get("duration")

    dest = _assets("web") / f"{attachment.uid}.mp4"
    # blocking-ok: worker-only, and it runs before the transcode is spawned — nothing else is
    # pending on this loop to be delayed by it.
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
            # blocking-ok: STAYS inline on purpose. This `finally` also runs on worker-shutdown
            # cancellation, and an await here could be interrupted before the unlink ever
            # happened — leaking a partial transcode (the biggest file the worker writes, and
            # nothing else reaps a `.tmp.mp4`). A synchronous call cannot be cut short.
            tmp.unlink(missing_ok=True)
        # The progress row is deliberately NOT cleared here. This runs inside the claiming
        # transaction, before the outcome commits, and in that window the snapshot would fall back
        # to the job row's committed 0 while the attachment still reads `processing` — a transcode
        # that visibly snapped from 100% back to 0% before turning ready. worker.run_once clears it
        # after the commit, and again at claim time for the attempt a kill left behind (MEMNG-36).
    if timed_out:
        raise RuntimeError(f"slow-lane transcode timed out after {ceiling}s")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)[-300:]}")

    # The delete endpoint does not wait for this job, so the attachment may have gone away during
    # the transcode. Check under the row lock BEFORE publishing: past this point the tmp file
    # becomes the served web tier, and republishing it after a delete would resurrect it.
    await guard_publish(db, job, attachment)  # sweeps tmp + any tier files when the delete won
    # blocking-ok: the publish must follow guard_publish's row-lock check with no yield point in
    # between — an await here would hand the loop back between "the attachment still exists" and
    # the rename that resurrects its file. Worker-only either way.
    os.replace(tmp, dest)  # atomic; poster was written by the fast lane first
    _stamp_mtime(dest, attachment)  # blocking-ok: one utime, same no-yield window as the rename
    job.progress_pct = 100
    attachment.status = AttachmentStatus.ready
    await db.flush()


async def process_printready_job(db: AsyncSession, job: MediaJob, attachment: Attachment) -> None:
    """Run one on-demand print rendition (MEMNG-49) — the second kind of slow-lane image work.

    Slow lane and thermally deferred like the transcode, for the same reason: a 40-60 MP decode +
    LANCZOS reduction + q92 encode is seconds of full-core CPU on a NAS that is shared with
    production and bounded by heat, and a book asks for a few hundred of them at once. The worker
    drains one job at a time and Pillow encodes on the calling thread, so "single-threaded and
    throttled" is what this already is — nothing here fans out.

    It publishes an EXTRA tier for a photo that is already `ready`, so unlike every other job it
    changes no attachment column and no status: a print rendition that never gets made must not
    make the photo itself look broken (see worker._fail_attachment).
    """
    # blocking-ok: a sysfs thermal-zone read (/sys/class/thermal), not the data volume — same
    # guard process_slow takes, and for the same host.
    if too_hot():
        raise ThermalDefer
    if not attachment.mime.startswith("image/"):
        # enqueue_printready filters non-images out; reaching here means a hand-made job row.
        raise RuntimeError(f"printready is image-only: attachment {attachment.id} is not an image")
    # blocking-ok: this IS the worker's job — a CPU-bound decode/encode that would still occupy
    # the process in a thread. Nothing else is queued on this loop to yield to.
    process_printready(attachment)
    # The delete endpoint never waits for a running job, so the photo may have been deleted while
    # this ran. Checked under the row lock, which also sweeps the file just published.
    await guard_publish(db, job, attachment)


async def process_job(db: AsyncSession, job: MediaJob) -> None:
    """Dispatch a media job to its lane. Raises on failure (caller marks it failed);
    raises ThermalDefer to re-queue without counting a failure."""
    attachment = (
        await db.execute(select(Attachment).where(Attachment.id == job.attachment_id))
    ).scalar_one_or_none()
    if attachment is None:
        # Deleted before this job got its turn: delete_attachment removes every job row it can
        # lock. A `processing` claim it could not remove is recovered by recover_stale_media_claims;
        # a job discarded here on finding the attachment gone is worker.run_once's AttachmentGone
        # path (MEMNG-41).
        raise AttachmentGone(f"attachment {job.attachment_id} no longer exists")

    # Release the read transaction BEFORE the (up to SLOW_TRANSCODE_TIMEOUT_S) derivation below, so
    # no DB session sits idle-in-transaction across the transcode — the kill that wedged the queue
    # (MEMNG-90). The claim is already durable (run_once committed `processing`); the result is
    # written in a fresh transaction that guard_publish re-locks (job row, then attachment row).
    await db.commit()

    # Dispatched on KIND first: a printready job is slow-lane image work, so the lane+mime rules
    # below would otherwise route it into the HEIC branch and re-derive the photo's own tiers.
    if job.kind == JobKind.printready:
        await process_printready_job(db, job, attachment)
        return

    if job.lane is JobLane.slow:
        if attachment.mime.startswith("video/"):
            await process_slow(db, job, attachment)
        else:
            # Heavy image (HEIC/AVIF): the CPU-costly libheif decode + derivative generation runs
            # on the slow (Merkur) worker. process_image never touches the original (ADR-0008).
            # blocking-ok: this IS the worker's job — a CPU-bound decode that would still occupy
            # the process in a thread. Nothing else is queued on this loop to yield to.
            meta = process_image(attachment)
            await guard_publish(db, job, attachment)
            attachment.width = meta.get("width")
            attachment.height = meta.get("height")
            attachment.blurhash = meta.get("blurhash")
            attachment.status = AttachmentStatus.ready
            await db.flush()
        return

    is_video = attachment.mime.startswith("video/")
    # blocking-ok: the fast lane's own work (Pillow decode / ffprobe+poster), same reasoning as
    # the slow-lane branch above — CPU-bound, worker-only, nothing concurrent to starve.
    meta = process_video_poster(attachment) if is_video else process_image(attachment)

    await guard_publish(db, job, attachment)
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
