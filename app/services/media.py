import asyncio
import contextlib
import json
import os
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import pi_heif
import structlog
from anyio import to_thread
from PIL import Image, ImageChops, ImageCms, ImageFile, ImageOps, ImageStat
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
    MailSubsampling,
    MediaJob,
    MediaJobProgress,
)
from app.services import (
    blurhash_pure,
    event_stream,
    export_settings,
    poster,
    printready,
    sibling,
)

pi_heif.register_heif_opener()  # lets PIL open HEIC/HEIF camera originals

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
# The long edge and the quality are no longer constants here: they are two of the four fields of a
# PRINT PROFILE (MEMNG-238), resolved per request from `export_settings` and carried into this
# function as a value — because the file's name and its cache directory are derived from the same
# four fields, and a constant read here could not agree with them. Their DEFAULTS still say 4096
# and q92, and they live once, in app/services/printready.py, which the app process can import
# without pulling in Pillow.
# The two below are not profile fields: they are print correctness, true of every profile.
PRINTREADY_SUBSAMPLING = 0  # 4:4:4 — keep full chroma resolution
PRINTREADY_DPI = (300, 300)
# Grace between SIGTERM and SIGKILL when stopping a runaway transcode: ffmpeg closes the output
# file and exits on TERM, so this only has to cover finishing the frame in flight.
KILL_GRACE_SECONDS = 5.0
GROUP_POLL_SECONDS = 0.25  # cadence of the "is anything left in the group?" probe
# The stop path's bound for the sweep a waited-out encode leaves behind (_discard_on_stop_if_gone),
# sized like worker.CANCEL_PROGRESS_CLEAR_SECONDS: a database or a volume that does not answer must
# not spend the grace the requeue after it has already earned (RELEASING.md, the stop budget).
CANCEL_SWEEP_TIMEOUT_SECONDS = 1.0
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
    # Every file this uid owns in a NON-default print profile, enumerated — finals AND the
    # `.jpg.tmp` an interrupted encode leaves behind, which the enumeration matches by prefix. The
    # set of profiles that ever existed is unbounded (MEMNG-238), so a derived list could only name
    # the ones settings happen to say today and would leave the rest behind.
    #
    # Unlike reclaim, a scan failure here is logged and swept around rather than raised: this list
    # feeds `discard_outputs`, whose caller has already established that the attachment row is
    # GONE. Failing the job would neither bring the row back nor remove anything, while going on
    # still reaps every other tier. The unreadable directory is named in the log.
    profiles: list[Path] = []
    try:
        profiles = printready.profile_variant_paths_sync(uid)
    except OSError as exc:
        logger.warning("printready_profile_scan_failed", uid=uid, error=str(exc))
    return [
        _assets("thumbs") / f"{uid}.jpg",
        _assets("thumbs") / f"{uid}.jpg.tmp",  # _atomic_save
        _assets("mobile") / f"{uid}.jpg",
        _assets("mobile") / f"{uid}.jpg.tmp",
        print_ready,
        print_ready.with_suffix(print_ready.suffix + ".tmp"),  # _atomic_save
        *profiles,
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


# The write buffer an OPTIMIZED save needs (MEMNG-241).
#
# `optimize=True` — and `progressive` — makes libjpeg run a pass over the whole scan, and that pass
# cannot suspend: everything it produces has to fit the single buffer Pillow hands the encoder.
# Pillow sizes that buffer by a heuristic and not by a bound (`JpegImagePlugin._save`: one byte per
# pixel below quality 95, two at or above, four for CMYK; then `ImageFile._save`:
# `max(MAXBLOCK, bufsize, width * 4)`), so a scan that costs more than the heuristic guessed makes
# libjpeg print "Suspension not allowed here" and Pillow raise
# OSError("broken data stream when writing image file") — the derive failing for an image that
# saves fine without `optimize`.
#
# TWO things the pixel count misses, and both are load-bearing:
#
#   * PADDING. A JPEG codes whole MCUs, so each component is padded up to the MCU size before it
#     is coded (8 px with 4:4:4, 16 px with 4:2:0 chroma; 16 is used here because it is the larger
#     and every profile has to fit the same bound). A NARROW picture is mostly padding and pays for
#     all of it: 2x5000 px is 10 000 pixels but 625 MCU rows, and at q95 4:4:4 it encodes to
#     72 609 bytes — past four bytes per PIXEL, and past Pillow's floor as well.
#   * The FLOOR. Pillow never goes below MAXBLOCK (65 536 by default), which is why small images
#     get away with the heuristic at all. The bound below keeps that floor rather than computing
#     its way under it, so a tiny image is never handed a smaller buffer than Pillow would.
#
# Four bytes per padded pixel is the FIRST rung, measured rather than guessed: on this build
# (Pillow 12.3.0, libjpeg-turbo 3.1.4.1) at 4:4:4 the densest inputs an 8-bit source can carry
# encode to 1.75 B/px (uniform noise, q92) through 3.22 B/px (an alternating-extremes pattern,
# q100). It is not a PROOF of sufficiency, and no such bound is derivable from what libjpeg
# documents — the coefficient coding alone allows ~10 B/px for three components before byte
# stuffing — so the encode grows its buffer and retries rather than trusting the first number:
# doubling up to 16 bytes per padded pixel, after which the OSError is raised as before. Each
# attempt encodes into a FRESH sink, so no half-written file survives a rung that failed.
_OPTIMIZED_JPEG_BYTES_PER_PIXEL = 4
_OPTIMIZED_JPEG_BYTES_PER_PIXEL_CAP = 16
_JPEG_MCU = 16  # the larger of the two MCU sizes (4:2:0); 4:4:4 pads to 8 and fits inside it
_JPEG_MIN_BUFFER = 65536  # ImageFile.MAXBLOCK's own default, kept as the floor
_JPEG_BUFFER_OVERFLOW = "broken data stream"  # Pillow's wording for the failed encode

# `ImageFile.MAXBLOCK` is a module attribute, so raising it is visible process-wide while it is in
# effect. The raised value harms nothing (a bigger buffer changes no byte of the output, and the
# size is read once per save, before any encoding), but the RESTORE would: two overlapping
# optimized saves would each put back the value they found, and whichever finished last would put
# the inflated one back for good. This lock is what keeps the global exactly as it was found. It
# serialises optimized encodes and nothing else — the screen tiers do not take it.
_MAXBLOCK_LOCK = threading.Lock()


def _jpeg_buffer_bound(img: Image.Image, params: Mapping[str, Any], bytes_per_pixel: int) -> int:
    """How many bytes to let the encoder write, for `img` saved with `params`.

    The area is the MCU-PADDED one, because that is what actually gets coded, and the metadata the
    save carries is added on top of it: an ICC profile and an EXIF block are written into the same
    buffer as the scan.
    """
    padded_width = -(-img.width // _JPEG_MCU) * _JPEG_MCU
    padded_height = -(-img.height // _JPEG_MCU) * _JPEG_MCU
    metadata = sum(
        len(params[key])
        for key in ("exif", "icc_profile")
        if isinstance(params.get(key), bytes | bytearray)
    )
    return max(_JPEG_MIN_BUFFER, bytes_per_pixel * padded_width * padded_height + metadata)


def _encode_jpeg(img: Image.Image, fp: "Path | BytesIO", buffer_bytes: int, **params: Any) -> None:
    """One encode attempt into a FRESH sink, with the write buffer raised to `buffer_bytes`.

    Fresh because an attempt that overflows has already written most of a file: reopening the path
    (or truncating the buffer) is what keeps a failed rung from prefixing the next one's output.
    """
    with _MAXBLOCK_LOCK:
        previous = ImageFile.MAXBLOCK
        ImageFile.MAXBLOCK = max(previous, buffer_bytes)
        try:
            if isinstance(fp, Path):
                with fp.open("wb") as handle:
                    img.save(handle, "JPEG", **params)
            else:
                fp.seek(0)
                fp.truncate()
                img.save(fp, "JPEG", **params)
        finally:
            ImageFile.MAXBLOCK = previous


def _save_jpeg(img: Image.Image, fp: Path | BytesIO, **params: Any) -> None:
    """`img.save(fp, "JPEG", ...)`, with a write buffer big enough for the encode it asks for.

    Every optimized save in this module goes through here rather than sizing the buffer at its own
    call site: the print tier alone has two of them (the default rung and every settings-chosen
    profile) and the mail rendition a third, and a per-site copy would leave whichever one nobody
    remembered raising on the same photo.
    """
    if not (params.get("optimize") or params.get("progressive")):
        img.save(fp, "JPEG", **params)  # a plain baseline save streams; it needs no headroom
        return
    bytes_per_pixel = _OPTIMIZED_JPEG_BYTES_PER_PIXEL
    while True:
        try:
            _encode_jpeg(img, fp, _jpeg_buffer_bound(img, params, bytes_per_pixel), **params)
            return
        except OSError as exc:
            # Only the overflow is retried, and only up to the cap: any other OSError (a full disk,
            # a vanished directory) is the caller's to see, immediately and unchanged.
            if (
                _JPEG_BUFFER_OVERFLOW not in str(exc)
                or bytes_per_pixel >= _OPTIMIZED_JPEG_BYTES_PER_PIXEL_CAP
            ):
                raise
            bytes_per_pixel *= 2


def _atomic_save(
    img: Image.Image, dest: Path, *, quality: int = 80, tmp: Path | None = None, **params: Any
) -> None:
    """Encode to a sibling temp and rename it into place, so no reader ever sees a partial tier.

    `params` passes the extra JPEG knobs the print rendition needs (subsampling, dpi, icc_profile)
    straight through to Pillow; the screen tiers pass none and keep their encode byte-for-byte.

    `tmp` names the in-flight file explicitly for the one caller that has to REAP it by name after
    a rolled-back request (the poster override, whose placement is arbitrated by a PosterCopy).
    Everyone else keeps the sibling `.tmp`, which only the job's own sweep ever enumerates.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if tmp is None:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        _save_jpeg(img, tmp, quality=quality, **params)
    except BaseException:
        # An encode that gave up has left most of a JPEG behind. Nothing reads a `.tmp` — only the
        # job's own sweep enumerates them — but leaving one costs the disk and lets a later reader
        # of the directory guess wrong about what is there.
        tmp.unlink(missing_ok=True)
        raise
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
    """Derive thumb + mobile tiers + blurhash + dims via Pillow (+pi-heif for HEIC).

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


# --- monochrome detection (MEMNG-238) ---------------------------------------------------------
#
# The reference set that motivated this: a professional lab's B&W exports are byte-identical in
# pixels to their colour twins but 5-7x smaller, and measurement showed WHY — the lab's B&W preset
# drops to q91/4:2:0. That is a quality cut and is explicitly not imitated. What IS imitated is the
# half of the saving that costs nothing: a neutral monochrome photo carries two chroma planes of
# nothing, and a single-channel JPEG simply does not have them. Same quality, fewer planes.
#
# The classification has to tell NEUTRAL from TONED, because sepia and split-toning ARE the picture:
# encoding them single-channel would throw the toning away and hand back a grey image. Both look
# monochrome; the difference is that toning has non-zero chroma, and it is UNIFORM (one hue over
# the whole frame) where a colour photograph's chroma varies.
DETECT_SAMPLE_EDGE = 256  # the downscale detection runs on: enough hue, ~65k pixels of work
# Chroma is measured in CIELAB as |a-128| + |b-128| per pixel (8-bit LAB, neutral = 128). A truly
# neutral image scores 0. Both neutral thresholds are deliberately tight — misreading a toned photo
# as neutral DESTROYS it, while misreading a neutral one as colour costs a few percent of file
# size, so every uncertain case must fall to the RGB side.
NEUTRAL_MEAN_CHROMA_MAX = 2.0
NEUTRAL_CHROMA_STDDEV_MAX = 2.0
# What separates TONED from COLOUR is not how much chroma there is, it is whether it all points the
# same way. Toning is ONE HUE whose strength varies with luminance (a sepia's shadows are near
# neutral and its midtones are warm), so its signed mean chroma is nearly its mean MAGNITUDE — the
# vectors do not cancel. A colour photograph's hues cancel: measured on synthetic photographs,
# sepia and split-toning score 1.00 and 0.96 while a colour frame scores 0.17.
TONED_UNIFORMITY_MIN = 0.85

Chromaticity = Literal["neutral", "toned", "color"]


@dataclass(frozen=True, slots=True)
class ChromaStats:
    """What a downscaled sample says about an image's colour, in three numbers.

    `magnitude` is the mean per-pixel chroma and `spread` its standard deviation — together they
    answer "is there any colour here at all". `uniformity` is the length of the MEAN chroma vector
    over `magnitude`: 1.0 when every pixel's hue points the same way (toning), near 0 when the
    hues cancel (a colour photograph). Separating the two questions is the point — a sepia photo
    has plenty of chroma AND plenty of spread, so no threshold on the first two can tell it from a
    landscape.
    """

    magnitude: float
    spread: float
    uniformity: float


def chroma_stats(img: Image.Image) -> ChromaStats:
    """Measure the colour of a downscaled sample of `img`.

    Pillow only, and no per-pixel Python: the sample is converted to LAB, each chroma channel is
    folded to its distance from neutral through a 256-entry lookup table, the two are added into
    one chroma image, and `ImageStat` reads the numbers off histograms. The addition cannot clip —
    each term is at most 127.

    `img` must already be RGB in sRGB (see `process_printready`): chroma statistics of a CMYK,
    wide-gamut or 16-bit buffer are not comparable to these thresholds.
    """
    sample = img.copy()
    sample.thumbnail((DETECT_SAMPLE_EDGE, DETECT_SAMPLE_EDGE))
    _, a_channel, b_channel = sample.convert("LAB").split()
    distance_from_neutral = [abs(value - 128) for value in range(256)]
    chroma = ImageChops.add(
        a_channel.point(distance_from_neutral), b_channel.point(distance_from_neutral)
    )
    stat = ImageStat.Stat(chroma)
    magnitude, spread = stat.mean[0], stat.stddev[0]
    # SIGNED means, which is what makes this a direction rather than a second magnitude.
    signed_a = ImageStat.Stat(a_channel).mean[0] - 128
    signed_b = ImageStat.Stat(b_channel).mean[0] - 128
    uniformity = (abs(signed_a) + abs(signed_b)) / magnitude if magnitude else 0.0
    return ChromaStats(magnitude=magnitude, spread=spread, uniformity=uniformity)


def classify_chromaticity(img: Image.Image) -> Chromaticity:
    """Is this picture neutral monochrome, toned monochrome, or colour?

    `neutral` is the only verdict that changes the encode. `toned` is named separately rather than
    lumped in with `color` because it is the verdict that has to be got RIGHT — a sepia print is
    monochrome to the eye, and the naive test ("are the channels equal?") calls it colour while a
    "not much chroma" test calls it neutral and greys it. So the distinction is stated in the code
    rather than left implicit in a threshold.
    """
    stats = chroma_stats(img)
    if stats.magnitude <= NEUTRAL_MEAN_CHROMA_MAX and stats.spread <= NEUTRAL_CHROMA_STDDEV_MAX:
        return "neutral"
    return "toned" if stats.uniformity >= TONED_UNIFORMITY_MIN else "color"


_SIXTEEN_BIT_MODES = frozenset({"I", "I;16", "I;16B", "I;16L", "I;16N"})
# The two date tags a `keep_dates` rendition carries, by EXIF number: DateTimeOriginal (when the
# shutter fired) and DateTimeDigitized/CreateDate. Both live in the Exif sub-IFD.
EXIF_SUB_IFD = 0x8769
DATE_TAGS = (0x9003, 0x9004)


def _normalise_for_print(img: Image.Image) -> Image.Image:
    """An arbitrary decoded source reduced to 8-bit with alpha resolved, before anything measures
    or encodes it.

    Two sources would otherwise be silently wrong:

    * **16-bit** (`I;16` and its endian spellings, a 16-bit PNG or TIFF). Pillow's `convert("RGB")`
      treats the value as an 8-bit sample and CLIPS: measured here, 16384/32768/65535 all come back
      255, so a 16-bit scan would print as a blown-out white field. Scaling by 1/256 first is the
      conversion that keeps the picture — but `point` is implemented for the NATIVE `I;16` only and
      raises `ValueError: point operation not supported for this mode` on the `I;16B` a big-endian
      TIFF decodes to, which would exhaust the job's retries on a perfectly good file. Going
      through 32-bit `I` first is what makes the scaling work for every spelling: measured,
      `I;16B → I` preserves the full 0/16384/32768/65535 range, while `I;16B → I;16` and
      `I;16B → L` both collapse it.
    * **Alpha** (`RGBA`, `LA`, palette transparency). `convert("RGB")` drops the alpha channel and
      keeps whatever RGB was hiding underneath it, which is arbitrary — a transparent PNG can
      carry any colour in its invisible pixels. They are composited onto WHITE instead, the colour
      of the paper, before either detection or encoding sees them.
    """
    if img.mode in _SIXTEEN_BIT_MODES:
        # `convert("I")` normalises every endian spelling into one 32-bit integer buffer that
        # `point` accepts; the float factor is the documented 16-bit reduction, and the `convert`
        # after it makes the result an ordinary 8-bit greyscale image.
        return img.convert("I").point(lambda value: value * (1 / 256)).convert("L")
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        composed = img.convert("RGBA")
        background = Image.new("RGBA", composed.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, composed).convert("RGB")
    return img


def _date_only_exif(source: Image.Image) -> bytes | None:
    """A FRESH EXIF block carrying only this source's date tags, or None when it has none.

    Rebuilt, never passed through, and that is the whole point of the policy: the source's own EXIF
    carries GPS coordinates, the camera's make/model/serial and an Orientation tag that is already
    baked into the pixels here — and this file is about to be uploaded to a print service. A
    whitelist that is constructed rather than filtered cannot leak a tag nobody thought to remove.
    """
    incoming = source.getexif().get_ifd(EXIF_SUB_IFD)
    kept = {tag: incoming[tag] for tag in DATE_TAGS if tag in incoming}
    if not kept:
        return None
    rebuilt = Image.Exif()
    rebuilt.get_ifd(EXIF_SUB_IFD).update(kept)
    return rebuilt.tobytes()


def process_printready(
    attachment: Attachment, profile: printready.PrintProfile | None = None
) -> Path:
    """Derive the print rendition of one photo and publish it. Returns the file it wrote.

    Source preference is the point of the tier: the PRINT MASTER when the photo has one — that is
    the user's own export for print, crop and output sharpening included — and only otherwise the
    original (app/services/printready.py:source_path). Never a screen tier.

    Downscaled with LANCZOS rather than `thumbnail`'s default bicubic: this is a 60 MP → 4096 px
    reduction whose result is inspected at 300 dpi on gloss paper, where bicubic's softness is the
    one artefact a reader can actually see. `thumbnail` never enlarges, so a source already at or
    below the cap keeps its own dimensions — upscaling would invent detail and print worse than
    the honest smaller image. A profile with no cap skips the resize entirely.

    THE ORDER IS THE CONTRACT. Orientation is baked, the buffer is normalised to 8-bit with alpha
    resolved, it is colour-MANAGED into sRGB, and only then is it measured: chroma statistics of a
    wide-gamut or 16-bit buffer are not comparable to the thresholds they are judged against, and
    a transparent pixel's hidden colour would skew them. The `L` image is created after the
    verdict, never before — building it first would make the classification a formality.

    A single-channel rendition is written UNTAGGED. The profile this pipeline embeds is a
    three-component sRGB one, and putting it in a one-component JPEG is a mismatch every reader is
    entitled to reject; an untagged grayscale JPEG is read as gamma 2.2, which is the sRGB tone
    curve to within a hair. That is a deliberate, recorded trade, not an omission.
    """
    resolved = profile or printready.DEFAULT_PROFILE
    source = printready.source_path(attachment)
    if source is None:  # enqueue refuses these; a hand-made job must fail loudly, not silently
        raise SourceMissing(f"attachment {attachment.id} has no source file for a print rendition")
    dest = printready.printready_path(attachment.uid, resolved)
    with Image.open(source) as raw:
        img = ImageOps.exif_transpose(raw)  # orientation first — the same path the tiers use
        img = _normalise_for_print(img)
        img, icc = to_srgb(img)
        if resolved.long_edge != printready.ORIGINAL_LONG_EDGE:
            img.thumbnail((resolved.long_edge, resolved.long_edge), Image.Resampling.LANCZOS)
        keep_dates = resolved.metadata_policy == printready.METADATA_KEEP_DATES
        exif = _date_only_exif(raw) if keep_dates else None
        if resolved.grayscale_single_channel and classify_chromaticity(img) == "neutral":
            img, icc = img.convert("L"), None
        params: dict[str, Any] = {
            "quality": resolved.jpeg_quality,
            "subsampling": PRINTREADY_SUBSAMPLING,
            "optimize": True,
            "dpi": PRINTREADY_DPI,
            "icc_profile": icc,
        }
        if exif is not None:
            params["exif"] = exif
        _atomic_save(img, dest, **params)
    _stamp_mtime(dest, attachment)  # carries the capture date, like every other derived tier
    return dest


# --- the mail rendition (MEMNG-240) -------------------------------------------------------------
#
# The digest's inline `cid:` images are encoded HERE rather than attached as a derived tier,
# because an inline mail image is a different picture from a screen tier: it is read at ~540 CSS
# px in a client that will never negotiate a format, it is base64-expanded by 4/3 into a message
# with a hard part cap, and it must survive a legacy Outlook that cannot decode a progressive
# JPEG. The four decisions that make it that picture are the MAIL PROFILE (app/services/
# export_settings.py) and are resolved from settings once per run — never here, and never per
# image: this function is handed the profile as a value, exactly like `process_printready`.
#
# It is a re-encode of the `mobile` tier, not of the original: the tier is already oriented,
# already 8-bit and already close to the target size, so the expensive part of a print derive (a
# 60 MP decode) is not paid for a photo that is about to be looked at on a phone.

# The mail subsampling, in Pillow's spelling. Keyed by the enum rather than by bare strings so a
# renamed member breaks here instead of silently falling through to a default.
MAIL_SUBSAMPLING: dict[str, int] = {
    MailSubsampling.C444: 0,  # 4:4:4 — no chroma reduction
    MailSubsampling.C420: 2,  # 4:2:0 — half chroma in both axes
}


@dataclass(frozen=True, slots=True)
class MailImage:
    """One encoded inline image: the bytes the message carries and the size they decode to.

    The size travels WITH the bytes because the template has to state it: an `<img>` with no
    height attribute collapses to nothing in the clients that block images until asked, and the
    only honest height is the one the encoder actually produced — the profile's long edge is a
    cap, not a dimension, and `thumbnail` never enlarges.
    """

    data: bytes
    width: int
    height: int


def encode_for_mail(source: bytes, profile: export_settings.MailProfile) -> MailImage | None:
    """One derived-tier JPEG re-encoded for the mail, or None when it cannot be read.

    None rather than an exception for a source that will not decode: a truncated or half-written
    tier must cost ONE photo its place on its card (the same degradation a missing file already
    causes) and never the whole digest. A profile this deployment cannot encode is the other case
    and DOES raise — an unknown subsampling can only come from a hand-edited settings row, and
    quietly substituting a default would ship bytes nobody asked for.

    The order is the same contract `process_printready` states: orientation is baked first, the
    buffer is normalised to 8-bit with alpha resolved, it is colour-MANAGED into sRGB (a
    Display-P3 phone photo is dull and desaturated without it), and only then is it downscaled and
    encoded. `optimize=True` costs a Huffman pass and buys a few percent of a budget measured
    against a hard cap.
    """
    subsampling = MAIL_SUBSAMPLING.get(profile.subsampling)
    if subsampling is None:
        raise ValueError(f"unknown mail subsampling {profile.subsampling!r}")
    try:
        with Image.open(BytesIO(source)) as raw:
            img = ImageOps.exif_transpose(raw)
            img = _normalise_for_print(img)  # not print-specific: 8-bit, alpha onto white
            img, icc = to_srgb(img)
            img.thumbnail((profile.long_edge, profile.long_edge), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            _save_jpeg(
                img,
                buffer,
                quality=profile.jpeg_quality,
                subsampling=subsampling,
                progressive=profile.progressive,
                optimize=True,
                icc_profile=icc,
            )
            return MailImage(data=buffer.getvalue(), width=img.width, height=img.height)
    except (OSError, ValueError) as exc:
        # OSError covers Pillow's "cannot identify image file" and a truncated decode; ValueError
        # covers a mode the encoder refuses. Neither names the file, because the log line is read
        # off a production host and the uid is already in the run's own record.
        logger.warning("digest.mail_image_unreadable", error=str(exc))
        return None


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
# Every ffmpeg command this repo runs carries these, and they are a PRIVACY control, not a tidy-up:
# a QuickTime original's `com.apple.quicktime.location.ISO6709` (or an MP4's `©xyz`) is copied into
# the output by ffmpeg's default metadata mapping, so before this the derived web tier of a
# geotagged clip published the household's coordinates to anyone the tier URL reached. A derived
# artifact carries no metadata at all; the archive master keeps everything (ADR-0008).
METADATA_STRIP_ARGS = ("-map_metadata", "-1", "-map_chapters", "-1")


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


# The binaries the job lanes spawn, by their BARE names, resolved on PATH. A spawn that cannot find
# one raises FileNotFoundError whose `filename` is exactly that bare name, while a missing source
# names the absolute path it was opened at — which is how worker.classify_failure tells a broken
# image from a missing original without reading a message (MEMNG-1162).
EXECUTABLES = frozenset({"ffmpeg", "ffprobe"})


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
    * `tonemap=tonemap=hable:desat=0` compresses the highlights. Hable, after trying both ways
      on two real HLG clips from this library: mobius held the midtones brighter but BLEW the
      highlights on the second clip — 6.6% of pixels above Y=250, a wall gone cream-yellow —
      which read on the device as an overdone HDR look, and no saturation trim touches luma.
      Hable clips essentially nothing on either clip (0.000%/0.001%) while restoring the same
      chroma, and that is the complaint this chain exists to fix. `desat=0` stops the filter
      from washing bright pixels towards white.
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
        "tonemap=tonemap=hable:desat=0,"
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
        *METADATA_STRIP_ARGS,
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


def sanitize_poster_override(src: Path, dest: Path, tmp: Path) -> None:
    """Publish a CLIENT-SUPPLIED poster as what it is: a derived asset, re-encoded and GPS-free.

    The override lands on the exact path the worker's hero frame would occupy and is served by the
    same signed poster tier, so it has to obey the same metadata policy as every other derived
    artifact — and it used to be a byte-for-byte copy, which published whatever the client's camera
    wrote, coordinates included. Re-encoding through the shared optimized save with
    `_date_only_exif` is what makes the two paths one policy rather than two.

    Orientation is baked into the pixels (`exif_transpose`) BEFORE the tag is dropped, exactly as
    the screen tiers do it: a rebuilt date-only EXIF carries no Orientation, so a file that relied
    on the tag would otherwise be published sideways.
    """
    with Image.open(src) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
        exif = _date_only_exif(raw)
        params: dict[str, Any] = {"exif": exif} if exif is not None else {}
        _atomic_save(img, dest, tmp=tmp, **params)


class ThermalDefer(Exception):
    """Raised to re-queue a slow job when the host is too hot (not a failure)."""


# Named failures, so `worker.classify_failure` reads a type rather than a message. Each stays a
# RuntimeError with the message it always had: callers and the stored `media_jobs.error` see no
# change (MEMNG-1046).
class TranscodeTimeout(RuntimeError):
    """The slow-lane transcode reached SLOW_TRANSCODE_TIMEOUT_S."""


class TranscodeFailed(RuntimeError):
    """ffmpeg exited non-zero."""


class SourceMissing(RuntimeError):
    """The job has no source file to derive from."""


# The package sensor is resolved BY NAME, never by hwmon index. /sys/class/hwmon/hwmonN is handed
# out in probe order and moves across boots, and on jupiter index 0 is `acpitz` — an ambient zone
# that sits near 28 °C and never moves — while the CPU package is a different chip entirely. That
# is how this guard spent the whole jupiter era reading a number that could not rise: a backfill
# ran with the package above 80 °C and deferred nothing.
#
# HWMON_ROOT is a module constant so a test can point the NAME RESOLUTION at a fake tree. It does
# not isolate a test on its own: a non-empty THERMAL_ZONE is read exactly as given and never goes
# near HWMON_ROOT, so a test that wants nothing of this machine patches HWMON_ROOT *and* leaves
# THERMAL_ZONE empty — which is what the suite's autouse fixture does. All these functions are
# pure reads with no cache and no shared state: two concurrent job consumers calling them at the
# same moment cannot interfere, and the scan is a handful of small sysfs reads against a directory
# the kernel keeps in memory — orders of magnitude cheaper than the transcode it guards, which is
# why there is deliberately no memoisation to invalidate or to race on.
HWMON_ROOT = Path("/sys/class/hwmon")
THERMAL_CHIP = "coretemp"  # Intel's per-package driver; jupiter's CPU
THERMAL_INPUT_LABEL = "Package id 0"  # the whole-package reading, not one core's
# The bound is a statement about what THIS guard reads — a CPU package sensor, or the host zone a
# THERMAL_ZONE override names — and not about sensors in general: 0 °C and readings above 150 °C
# are perfectly real elsewhere. A running machine's package is neither, so a reading outside the
# range here is a failure to measure and the guard answers UNKNOWN instead of a verdict. Both ends
# would otherwise read as a decision: a disconnected or misread sensor reports 0 or a negative
# sentinel, which lands below any threshold and says "cool" about a host nothing measured; and an
# absurd positive is permanently above any threshold, which would leave backfill_location's
# `while too_hot()` waiting for a machine that is never going to cool. (This package's own critical
# limit is 100 °C.) The bound applies to an explicit THERMAL_ZONE as well, so a sensor whose
# legitimate working range leaves 0–150 °C cannot be used with this guard.
THERMAL_MIN_PLAUSIBLE_MILLIDEG = 1
THERMAL_MAX_PLAUSIBLE_MILLIDEG = 150_000


class ThermalState(StrEnum):
    """What the guard can honestly say about the host.

    UNKNOWN is its own answer and never collapses into COOL: "no sensor" and "measured, and cool"
    are different facts, and the failure this exists to prevent is the second being reported when
    only the first is true.
    """

    COOL = "cool"
    HOT = "hot"
    UNKNOWN = "unknown"


def resolve_thermal_sensor() -> Path | None:
    """The CPU package temperature input on this host, or None when there is no such sensor.

    None is the honest answer on a dev machine, in a container without /sys, or on a CPU whose
    driver names things differently (Saturn's k10temp publishes no labels at all) — and it means
    UNKNOWN, not cool.

    Every read is guarded — the candidate's existence check included — and against ValueError as
    well as OSError: sysfs entries can disappear between the scan and the open, and a chip whose
    `name` or label is not valid UTF-8 raises
    UnicodeDecodeError, which is a ValueError and would otherwise escape this function entirely.
    The first caller is the worker's startup line, before the schema gate — an exception there does
    not degrade the guard, it stops the worker from starting. One unreadable chip is skipped; the
    scan goes on and can still find a good one.
    """
    try:
        chips = sorted(HWMON_ROOT.iterdir())
    except OSError:
        return None
    for chip in chips:
        try:
            if (chip / "name").read_text().strip() != THERMAL_CHIP:
                continue
            labels = sorted(chip.glob("temp*_label"))
        except (OSError, ValueError):
            continue
        for label in labels:
            try:
                if label.read_text().strip() != THERMAL_INPUT_LABEL:
                    continue
            except (OSError, ValueError):
                continue
            candidate = label.with_name(label.name.removesuffix("_label") + "_input")
            try:
                found = candidate.exists()
            except OSError:
                # exists() answers False for ENOENT, ENOTDIR, EBADF and ELOOP and re-raises every
                # other OSError — EACCES and EIO among them. Those are unreadable candidates like
                # any other, not a reason to take the worker down.
                continue
            if found:
                return candidate
    return None


def thermal_sensor() -> Path | None:
    """Which file the guard reads. An explicit THERMAL_ZONE wins, unresolved and unchecked: it is
    how a host this module cannot name its way to is configured, so it must not be second-guessed
    by the name resolution it exists to bypass."""
    if settings.THERMAL_ZONE:
        return Path(settings.THERMAL_ZONE)
    return resolve_thermal_sensor()


def read_temp_millideg() -> int | None:
    sensor = thermal_sensor()
    if sensor is None:
        return None
    try:
        milli = int(sensor.read_text().strip())
    except (OSError, ValueError):
        return None  # configured but unreadable is UNKNOWN too, not cool
    if not THERMAL_MIN_PLAUSIBLE_MILLIDEG <= milli <= THERMAL_MAX_PLAUSIBLE_MILLIDEG:
        return None  # a number, but outside the range this guard accepts — see the bound's comment
    return milli


def thermal_state() -> ThermalState:
    temp = read_temp_millideg()
    if temp is None:
        return ThermalState.UNKNOWN
    return ThermalState.HOT if temp > settings.THERMAL_MAX_C * 1000 else ThermalState.COOL


def too_hot() -> bool:
    """Whether work must be deferred. UNKNOWN is NOT a defer: a host with no sensor (dev, CI, a
    container without /sys) has to keep draining its queue, and the previous behaviour of this
    guard — silently failing open — is only dangerous when it is also silent. So the tri-state is
    reported separately by thermal_state(), and the worker says which one it started with."""
    return thermal_state() is ThermalState.HOT


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

    `-threads` appears twice because ffmpeg options are positional: before `-i` it is an INPUT
    option and bounds the decoder, after it an OUTPUT option and bounds libx264. The output one
    alone left decoding on ffmpeg's auto thread count, which on a 1080p120 source pushed a
    "one-thread" transcode to 3.8 cores (measured 2026-09-27, see WORKER_TRANSCODE_CPU_COST).
    """
    return [
        "ffmpeg",
        "-y",
        "-threads",
        str(threads),
        "-i",
        src,
        "-map",
        VIDEO_STREAM_MAP,
        "-map",
        "0:a:0?",
        *METADATA_STRIP_ARGS,
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
            await event_stream.notify_attachment_status_for_job(db, job_id)
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
            # Suppressed for the reason the checkpoint's unlink below gives, which this `finally`
            # shares: a stop unwinds THROUGH here, and an error raised while a CancelledError is
            # propagating replaces it exactly as one raised inside an `except` block would.
            with contextlib.suppress(OSError):
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
        raise TranscodeTimeout(f"slow-lane transcode timed out after {ceiling}s")
    if proc.returncode != 0:
        raise TranscodeFailed(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)[-300:]}")

    # The delete endpoint does not wait for this job, so the attachment may have gone away during
    # the transcode. Check under the row lock BEFORE publishing: past this point the tmp file
    # becomes the served web tier, and republishing it after a delete would resurrect it.
    try:
        await guard_publish(db, job, attachment)  # sweeps tmp + any tier files when the delete won
    except asyncio.CancelledError:
        # A stop delivered at guard_publish's own first await skips that sweep, and nothing else
        # reaps a `.tmp.mp4`: a delete that landed before ffmpeg created this one took the job row
        # with the attachment, so the cancel path's conditional requeue moves no row and the job
        # never runs again. Unlike the light lane's fence this asks the database nothing — the only
        # file this run has on disk is its own unpublished temp, which even a live attachment's
        # requeued run re-transcodes from scratch rather than resuming. That keeps the transcode
        # path's stop budget where it is (RELEASING.md): it can afford no further bounded second.
        # Suppressed, not just `missing_ok`: that covers a missing file and nothing else, and this
        # temp sits on the NFS-backed derived tier where EIO/ESTALE/EROFS are real. Raised from
        # inside this except block the error would REPLACE the cancellation — run_once would see an
        # ordinary failure and never run the cancel hand-back. A temp that stays is a leak a
        # SIGKILL would have left anyway; a replaced cancellation is a swallowed stop.
        with contextlib.suppress(OSError):
            # blocking-ok: one unlink, on a stop path where an await could be interrupted before it
            # ever happened — the same reason the `finally` above stays synchronous.
            tmp.unlink(missing_ok=True)
        raise
    # blocking-ok: the publish must follow guard_publish's row-lock check with no yield point in
    # between — an await here would hand the loop back between "the attachment still exists" and
    # the rename that resurrects its file. Worker-only either way.
    os.replace(tmp, dest)  # atomic; poster was written by the fast lane first
    _stamp_mtime(dest, attachment)  # blocking-ok: one utime, same no-yield window as the rename
    job.progress_pct = 100
    attachment.status = AttachmentStatus.ready
    await event_stream.notify_attachment_status(db, attachment.uid, attachment.owner_id)
    await db.flush()


async def _discard_on_stop_if_gone(attachment_id: int, uid: str) -> None:
    """The fence guard_publish would have been, for a thread the STOP waited out.

    The wait-out below lets the encode finish, which means it has already `os.replace`d its
    rendition into the final path — and then the cancellation is re-raised BEFORE guard_publish,
    the checkpoint that sweeps what a job published for a photo deleted meanwhile. Nothing sweeps
    it afterwards either: the delete takes the job row along with the attachment (the claim is
    committed before processing, so the row is lockable), so the cancel path's conditional requeue
    moves no row and the job never runs again. Without this, a stop that lands during the encode of
    a just-deleted photo leaves a multi-megabyte rendition on disk that nothing names.

    A session of its OWN, for the reason worker._requeue_cancelled_claim gives: the consumer's
    session is being torn down around this call. BOUNDED and SHIELDED for the same reasons too — a
    second stop must not interrupt a half-done sweep, and a database that does not answer must not
    hold the process past docker's grace. Past the bound the file stays, which is where a SIGKILL
    would have left it anyway. Every failure is swallowed on purpose: the caller re-raises the
    cancellation immediately, and an error from here would REPLACE the stop.

    The row check is guard_publish's own (locked, so a delete committing right now is waited for
    rather than read around), and the sweep itself goes to a thread: it is a dozen unlinks over
    NFS, and an unbounded blocking call here would defeat the bound above.
    """

    async def _sweep() -> None:
        async with async_session() as db:
            try:
                if await attachment_still_exists(db, attachment_id):
                    return  # a live photo's rendition: the requeued job publishes nothing new
            finally:
                await db.rollback()  # the row lock is held for the read alone
        await to_thread.run_sync(discard_outputs, uid)

    sweep = asyncio.ensure_future(_sweep())
    try:
        async with asyncio.timeout(CANCEL_SWEEP_TIMEOUT_SECONDS):
            await asyncio.shield(sweep)
    except (TimeoutError, asyncio.CancelledError):
        sweep.cancel()  # the bound reached, or a second cancel — let the stop proceed
        logger.warning("printready_cancel_sweep_unfinished", attachment_id=attachment_id, uid=uid)
    except Exception:
        logger.exception("printready_cancel_sweep_error", attachment_id=attachment_id, uid=uid)


async def _guard_publish_fenced(db: AsyncSession, job: MediaJob, attachment: Attachment) -> None:
    """guard_publish, plus the same fence for a stop delivered AT ITS OWN first await.

    guard_publish begins with `SELECT … FOR UPDATE` on the job row, so a stop can land inside it —
    between a publish that has already happened and the sweep that is supposed to follow it. The
    cancellation then comes out of this call and the checkpoint never gets to sweep anything, which
    is the permanent leak _discard_on_stop_if_gone describes: the delete took the job row with the
    attachment, so nothing requeues and no later run passes the checkpoint either.

    Every site that publishes before its checkpoint goes through here. The wait-out fences above
    are its other half and cannot both fire for one job: a stop that the wait-out absorbs re-raises
    before the checkpoint is ever reached, so the light path's stop budget still carries exactly
    one CANCEL_SWEEP_TIMEOUT_SECONDS (RELEASING.md).
    """
    try:
        await guard_publish(db, job, attachment)
    except asyncio.CancelledError:
        await _discard_on_stop_if_gone(attachment.id, attachment.uid)
        raise


async def process_printready_job(db: AsyncSession, job: MediaJob, attachment: Attachment) -> None:
    """Run one on-demand print rendition (MEMNG-49) — the second kind of slow-lane image work.

    Slow lane and thermally deferred like the transcode, for the same reason: a 40-60 MP decode +
    LANCZOS reduction + q92 encode is seconds of full-core CPU on a NAS that is shared with
    production and bounded by heat, and a book asks for a few hundred of them at once. The worker
    drains one slow-lane job at a time and the encode below occupies exactly one worker thread, so
    "single-threaded and throttled" is what this already is — nothing here fans out.

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
    # The profile comes off the JOB, never out of settings: minutes or hours may separate the
    # request from this line, and the bytes that get written have to be the ones that were asked
    # for — the cache path they land at was decided by the same key. NULL is the default profile
    # (every job written before the column existed produced the shipped tier).
    profile = printready.DEFAULT_PROFILE
    if job.profile_key is not None:
        parsed = printready.profile_from_key(job.profile_key)
        if parsed is None:
            # Only a hand-made row can get here: enqueue writes a canonical key. Fail loudly
            # rather than guess a profile and publish bytes nobody asked for.
            raise RuntimeError(f"job {job.id} names an unknown print profile {job.profile_key!r}")
        profile = parsed
    # Off the loop, because "nothing else is queued on this loop" stopped being true: the worker
    # process now runs TWO consumers plus the supervisor on one event loop. Held inline, a
    # multi-second 40-60 MP decode/LANCZOS/q92 encode freezes the transcode consumer's progress
    # writes and the supervisor's heartbeat for its whole duration — and a book asks for a few
    # hundred renditions in a row. The CPU cost is unchanged; only the loop is given back.
    # `attachment` may cross to the thread because process_printready reads nothing that can
    # touch the session: uid, storage_path, captured_at, id, and the selectin-loaded `renditions`
    # collection (app/models.py:283) — all already loaded, and expire_on_commit=False
    # (app/database.py:39) keeps them loaded across process_job's pre-derive commit.
    # The wait-out is NOT anyio's `abandon_on_cancel=False`: that shield only covers cancel-scope
    # cancellation, and the worker's teardown cancels each consumer task natively
    # (app/worker.py: `task.cancel()`), which goes straight through it. A thread cannot be
    # interrupted, so an abandoned encode keeps resizing and os.replace-ing the rendition into its
    # final path after the coroutine is gone — past the guard_publish below, which is what fences
    # a photo deleted mid-job. Shielding restores the ordering the inline call had for free: a
    # stop that lands during an encode is delivered only once the thread is done. A printready job
    # is claimed by the LIGHT consumer — the transcode class is slow-lane `derive` on a video
    # attachment and this is neither (app/worker.py:201-220) — and that consumer runs one job at a
    # time (WORKER_LIGHT_SLOTS=1), so a stop waits out exactly one rendition's remaining encode.
    # Nothing the thread does may escape that wait: a CancelledError from a second stop, and the
    # encode's own failure just as much, since either one leaving this block would REPLACE the
    # cancellation — and run_once would then book a job failure and keep the consumer looping with
    # its one cancellation already spent. A failed encode is dropped here on purpose: the job is
    # not marked, so it is simply re-run after the stop. The cancellation is re-raised, never
    # swallowed.
    encode = asyncio.create_task(to_thread.run_sync(process_printready, attachment, profile))
    try:
        await asyncio.shield(encode)
    except asyncio.CancelledError:
        while not encode.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(encode)  # re-shielded: a second stop must not win either
        if not encode.cancelled():
            encode.exception()  # retrieved, so a failed encode is not logged as never-retrieved
        # The thread published while the stop was waiting for it, and the guard_publish below is
        # never reached — so the fence it would have applied is applied here instead.
        await _discard_on_stop_if_gone(attachment.id, attachment.uid)
        raise
    # The delete endpoint never waits for a running job, so the photo may have been deleted while
    # this ran. Checked under the row lock, which also sweeps the file just published.
    await _guard_publish_fenced(db, job, attachment)


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
    # below would otherwise route it into the HEIC branch and re-derive the photo's own tiers. A
    # sibling job is fast-lane image work for the same reason: it must never re-derive anything.
    if job.kind == JobKind.sibling:
        await sibling.process_sibling_job(db, job)
        return
    if job.kind == JobKind.printready:
        await process_printready_job(db, job, attachment)
        return

    if job.lane is JobLane.slow:
        if attachment.mime.startswith("video/"):
            await process_slow(db, job, attachment)
        else:
            # Heavy image (HEIC/AVIF): the CPU-costly libheif decode + derivative generation runs
            # on the slow (Merkur) worker. process_image never touches the original (ADR-0008).
            # Off the loop for the reason process_printready_job gives, and in the same shape:
            # "nothing else is queued on this loop" stopped being true with two consumers and a
            # supervisor sharing it, and this branch is the LIGHT consumer's heaviest job — seconds
            # of decode per photo, a whole import back to back, during which the transcode
            # consumer's progress writes and the heartbeat cannot run. `attachment` crosses to the
            # thread reading only already-loaded columns, as it does there.
            # The shielded wait-out preserves what the inline call did on a stop exactly: a thread
            # cannot be interrupted, so the decode finishes and publishes its tiers either way, and
            # the cancellation is delivered only afterwards — never swallowed, and never leaving a
            # half-written tier behind.
            decode = asyncio.create_task(to_thread.run_sync(process_image, attachment))
            try:
                meta = await asyncio.shield(decode)
            except asyncio.CancelledError:
                while not decode.done():
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.shield(decode)  # re-shielded: a second stop must not win
                if not decode.cancelled():
                    decode.exception()  # retrieved: not logged as a never-retrieved failure
                # The thread published its tiers while the stop was waiting for it, and the
                # checkpoint below is never reached — so its fence is applied here instead, as
                # process_printready_job does for the rendition it waits out.
                await _discard_on_stop_if_gone(attachment.id, attachment.uid)
                raise
            await _guard_publish_fenced(db, job, attachment)
            attachment.width = meta.get("width")
            attachment.height = meta.get("height")
            attachment.blurhash = meta.get("blurhash")
            attachment.status = AttachmentStatus.ready
            await event_stream.notify_attachment_status(db, attachment.uid, attachment.owner_id)
            await db.flush()
        return

    is_video = attachment.mime.startswith("video/")
    # blocking-ok: the fast lane's own work (Pillow decode / ffprobe+poster), same reasoning as
    # the slow-lane branch above — CPU-bound, worker-only, nothing concurrent to starve.
    meta = process_video_poster(attachment) if is_video else process_image(attachment)

    # Fenced for the one window a stop has here: the decode above is inline, so the checkpoint's
    # own first await is the only place a cancellation can land after those tiers are published.
    await _guard_publish_fenced(db, job, attachment)
    attachment.width = meta.get("width")
    attachment.height = meta.get("height")
    attachment.duration = meta.get("duration")
    attachment.blurhash = meta.get("blurhash")
    # Image is complete after the fast lane; video still needs the slow-lane transcode.
    attachment.status = AttachmentStatus.processing if is_video else AttachmentStatus.ready
    await event_stream.notify_attachment_status(db, attachment.uid, attachment.owner_id)
    if is_video:
        # Enqueue the transcode only now that the fast lane (poster, dims, duration) is done,
        # which guarantees fast→slow ordering regardless of how many workers run.
        db.add(MediaJob(attachment_id=attachment.id, lane=JobLane.slow, state=JobState.queued))
    await db.flush()
