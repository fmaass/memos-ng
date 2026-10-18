"""Read one image's CAMERA facts from its own EXIF — never from its pixels (MEMNG-865).

The third reader beside `services/capture.py` (the date) and `services/location.py` (the place),
and built on the same mechanics: the raw EXIF payload is taken exactly where capture takes it —
`im.info["exif"]` for JPEG, the ISOBMFF box walker for HEIF/HEIC/AVIF, the eXIf chunk walker for
PNG, `tag_v2` for TIFF — and parsed with a standalone `Image.Exif`, so no path falls through to
`load()` and no pixel is decoded. Eight tags are read and nothing else: Make and Model out of IFD0,
LensModel, FNumber, ExposureTime, ISO, FocalLength and ExposureBias out of the Exif sub-IFD.
Video is not read at all — a stated deferral, not an oversight.

This module is the ONE guard between a file's bytes and an SQL parameter (fold F1). Every field
is validated here, on its own: a string loses its control characters and is capped, a number must
be finite and inside its range, the ISO must be an integer PostgreSQL's INTEGER can hold. An
invalid field becomes None and never discards a valid sibling, so whatever this returns can be
written and served as JSON without a second check anywhere.

Three answers, kept apart because the backfill resumes on them:

* a `CameraInfo` — at least one field survived (`'exif'`);
* `None` — the file was read and no field survived, including "every field present was invalid"
  (`'none'`, final);
* `UNREADABLE` — the read itself failed: a missing original, a file no decoder recognises, a parse
  that raised (`'unreadable'`, which `backfill_camera.py --retry-unreadable` comes back for).

A HEIF whose headers cannot address its Exif is NOT `None`: the box walker declining means "not
answerable from the headers" (`capture.heif_exif_from`), so the full open is the fallback. No
value out of the metadata is ever logged — only the mime and an exception's type.

A STRUCTURALLY broken EXIF is unreadable too, and Pillow does not say so by raising: a directory
that declares more entries than it holds, a value or sub-IFD offset past the payload's end, are
answered with a "Corrupt EXIF data" WARNING and a short or empty directory. So the directories are
loaded here (`_directory`) rather than through `Image.Exif`, and each one's completeness is read
off the directory object itself — never by capturing warnings, whose filters are process-wide
state that two reader threads (or a location read beside this one) would race on.
"""

import io
import math
import struct
import unicodedata
from collections.abc import Mapping
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import BinaryIO

import pillow_heif
import structlog
from PIL import Image, PngImagePlugin, TiffImagePlugin, WebPImagePlugin

from app.services import capture

pillow_heif.register_heif_opener()  # HEIC/HEIF primaries (also registered in services.media)

logger = structlog.get_logger()

# The whole whitelist. IFD0:
MAKE = 0x010F
MODEL = 0x0110
# The Exif sub-IFD (capture.EXIF_IFD, 0x8769):
LENS_MODEL = 0xA434
F_NUMBER = 0x829D
EXPOSURE_TIME = 0x829A
ISO = 0x8827
FOCAL_LENGTH = 0x920A
EXPOSURE_BIAS = 0x9204

# The `attachments.camera_source` ledger. NULL = never visited.
SOURCE_EXIF = "exif"
SOURCE_NONE = "none"  # read, and no camera field survived
SOURCE_UNREADABLE = "unreadable"  # the read failed or timed out — retryable
SOURCE_NOT_IMAGE = "not-image"  # classified by mime, never read

TEXT_CAP = 255
PG_INTEGER_MAX = 2147483647

# (low, high): the high bound is inclusive; the low bound is open except for the bias, whose
# range is [-100, 100] — zero and negative compensation are both real settings.
F_NUMBER_RANGE = (0.0, 1000.0)
EXPOSURE_TIME_RANGE = (0.0, 86400.0)
FOCAL_LENGTH_RANGE = (0.0, 10000.0)
EXPOSURE_BIAS_RANGE = (-100.0, 100.0)


@dataclass(frozen=True)
class CameraInfo:
    """What one image says about the camera that took it. Every field is already safe to store."""

    make: str | None = None
    model: str | None = None
    lens_model: str | None = None
    f_number: float | None = None
    exposure_time_s: float | None = None
    focal_length_mm: float | None = None
    exposure_bias_ev: float | None = None
    iso: int | None = None


class Unreadable:
    """The read failed — a different fact from "read, and nothing there". One instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE = Unreadable()

type CameraRead = CameraInfo | Unreadable | None


def is_image(mime: str) -> bool:
    return mime.startswith("image/")


def source_of(mime: str, found: CameraRead) -> str:
    """The ledger value for one visit. A non-image is classified by its mime alone."""
    if not is_image(mime):
        return SOURCE_NOT_IMAGE
    if isinstance(found, Unreadable):
        return SOURCE_UNREADABLE
    if found is None:
        return SOURCE_NONE
    return SOURCE_EXIF


def columns(found: CameraInfo | None) -> dict[str, str | float | int | None]:
    """The eight `attachments` camera columns for one answer — all None when there is none."""
    info = found or CameraInfo()
    return {
        "camera_make": info.make,
        "camera_model": info.model,
        "lens_model": info.lens_model,
        "f_number": info.f_number,
        "exposure_time_s": info.exposure_time_s,
        "focal_length_mm": info.focal_length_mm,
        "exposure_bias_ev": info.exposure_bias_ev,
        "iso": info.iso,
    }


def clean_text(raw: object) -> str | None:
    """A string tag as text safe for Postgres and JSON, or None.

    Bytes are decoded as UTF-8 with replacement. Every control character (C0, DEL, C1 — Unicode
    category Cc, NUL included) is removed, and so is a lone surrogate, which no UTF-8 encoder will
    hand to the database. Whitespace is stripped and the result capped at `TEXT_CAP` characters;
    nothing left is None.
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    kept = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cs"))
    kept = kept.strip()[:TEXT_CAP].rstrip()
    return kept or None


def clean_number(raw: object, low: float, high: float, *, low_inclusive: bool) -> float | None:
    """One EXIF number or rational as a finite float inside its range, or None.

    A zero denominator (Pillow's NaN rational), any conversion error and a non-finite result are
    all None, as is a bool, which `float()` would otherwise happily turn into 1.0.
    """
    if isinstance(raw, bool) or getattr(raw, "denominator", None) == 0:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]  # the try IS the type check
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    above_low = value >= low if low_inclusive else value > low
    return value if above_low and value <= high else None


def clean_iso(raw: object) -> int | None:
    """The ISO as an integer in [1, PostgreSQL INTEGER max], or None.

    ISOSpeedRatings may carry several values; the first is the sensitivity the photo was taken at.
    A string is refused rather than parsed: the tag is numeric, and a string in it is damage.
    """
    if isinstance(raw, tuple | list):
        if not raw:
            return None
        raw = raw[0]
    if isinstance(raw, bool | str | bytes):
        return None
    if isinstance(raw, int):
        value = raw
    else:
        if getattr(raw, "denominator", None) == 0:
            return None
        try:
            number = float(raw)  # type: ignore[arg-type]  # the try IS the type check
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            return None
        if not (math.isfinite(number) and number.is_integer()):
            return None
        value = int(number)
    return value if 1 <= value <= PG_INTEGER_MAX else None


def _tag_text(raw: object) -> object:
    """Pillow decodes an ASCII tag as Latin-1; hand its BYTES back so they are read as UTF-8."""
    if isinstance(raw, str):
        try:
            return raw.encode("latin-1")
        except UnicodeEncodeError:
            return raw
    return raw


def camera_from_tags(ifd0: Mapping[int, object], sub: Mapping[int, object]) -> CameraInfo | None:
    """The eight tags out of IFD0 and the Exif sub-IFD, each validated alone; None if none survive.

    Takes plain mappings so the per-field rules are testable without authoring a file.
    """
    info = CameraInfo(
        make=clean_text(_tag_text(ifd0.get(MAKE))),
        model=clean_text(_tag_text(ifd0.get(MODEL))),
        lens_model=clean_text(_tag_text(sub.get(LENS_MODEL))),
        f_number=clean_number(sub.get(F_NUMBER), *F_NUMBER_RANGE, low_inclusive=False),
        exposure_time_s=clean_number(
            sub.get(EXPOSURE_TIME), *EXPOSURE_TIME_RANGE, low_inclusive=False
        ),
        focal_length_mm=clean_number(
            sub.get(FOCAL_LENGTH), *FOCAL_LENGTH_RANGE, low_inclusive=False
        ),
        exposure_bias_ev=clean_number(
            sub.get(EXPOSURE_BIAS), *EXPOSURE_BIAS_RANGE, low_inclusive=True
        ),
        iso=clean_iso(sub.get(ISO)),
    )
    if all(value is None for value in astuple(info)):
        return None
    return info


class CorruptExif(ValueError):
    """An EXIF directory could not be read to its end — the file is damaged, not silent."""


# `ImageFileDirectory_v2.load` assigns `next` (the following IFD's offset) as the LAST step of a
# complete walk, and on a truncated read it warns and returns before reaching it. A value no real
# offset can take, set before the load, therefore survives exactly when the walk did not finish —
# a fact about this one object, so it is safe on any thread.
_UNFINISHED = -1


def _unwrap(value: object) -> object:
    """A one-element tuple as its element — what `Image.Exif` hands back for the same tag."""
    if isinstance(value, tuple) and len(value) == 1:
        return value[0]
    return value


def _directory(
    fp: BinaryIO, head: bytes, offset: object, group: int | None = None
) -> dict[int, object]:
    """One IFD at `offset` as {tag: value}, or CorruptExif when it cannot be read completely."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CorruptExif("directory offset")
    ifd = TiffImagePlugin.ImageFileDirectory_v2(head, group=group)
    fp.seek(offset)
    ifd.next = _UNFINISHED
    ifd.load(fp)
    if ifd.next == _UNFINISHED:
        raise CorruptExif("truncated directory")
    return {tag: _unwrap(value) for tag, value in ifd.items()}


# The two magic spellings Pillow accepts for a classic header — the correct one for the byte order
# and the swapped one its own reader tolerates — and BigTIFF's, which has only the correct one.
_CLASSIC_MAGIC = (b"\x2a\x00", b"\x00\x2a")


def _tiff_header(fp: BinaryIO) -> bytes:
    """The WHOLE header a directory object needs: 8 bytes for classic TIFF, 16 for BigTIFF.

    BigTIFF (magic 43) widens the header: offset size (8), a reserved word (0), and an 8-byte
    first-IFD offset at byte 8; reading only eight bytes of it left the offset missing. Pillow has
    no public entry point that parses a header, so this follows the format itself. A BigTIFF in
    big-endian (MM) order is refused as unreadable rather than misread: Pillow 12 decides
    "BigTIFF" from byte 2 alone, which is the HIGH byte of an MM magic, so it would walk such a
    file's 20-byte entries as 12-byte ones — and it cannot open such a file either.
    """
    head = fp.read(8)
    if len(head) < 8 or head[:2] not in (b"II", b"MM"):
        raise CorruptExif("TIFF header")
    endian = "<" if head[:2] == b"II" else ">"
    if head[2:4] in _CLASSIC_MAGIC:
        return head
    if struct.unpack(endian + "H", head[2:4])[0] != 43:
        raise CorruptExif("TIFF magic")
    if head[:2] == b"MM":
        raise CorruptExif("big-endian BigTIFF")
    if struct.unpack(endian + "HH", head[4:8]) != (8, 0):
        raise CorruptExif("BigTIFF header")
    rest = fp.read(8)
    if len(rest) < 8:
        raise CorruptExif("BigTIFF header")
    return head + rest


def _from_tiff_structure(fp: BinaryIO) -> CameraInfo | None:
    """IFD0 and the Exif sub-IFD out of a TIFF structure that starts at offset 0 of `fp`.

    The GPS IFD is never walked: IFD0 yields its offset as a number and nothing follows it.
    """
    fp.seek(0)
    head = _tiff_header(fp)
    first = TiffImagePlugin.ImageFileDirectory_v2(head)
    ifd0 = _directory(fp, head, first.next)
    sub_offset = ifd0.get(capture.EXIF_IFD)
    sub = {} if sub_offset is None else _directory(fp, head, sub_offset, capture.EXIF_IFD)
    return camera_from_tags(ifd0, sub)


def _from_payload(raw: bytes) -> CameraInfo | None:
    """A payload a container DECLARED as EXIF: nothing behind the prefix is a failed read.

    An empty remainder is not "no EXIF" — the container said there was some — so it goes to the
    header read like any other, which refuses it as unreadable.
    """
    while raw.startswith(capture.EXIF_PREFIX):
        raw = raw[len(capture.EXIF_PREFIX) :]
    return _from_tiff_structure(io.BytesIO(raw))


# The VP8X flag that says the file carries an EXIF chunk (WebP container spec, "Extended
# File Format": bit 3 of the first flags byte).
_VP8X_EXIF_FLAG = 0x08


def _webp_declares_exif(path: Path) -> bool:
    """Whether a WebP declares EXIF: an `EXIF` chunk, or the VP8X header's EXIF flag.

    Pillow drops an EMPTY `EXIF` chunk (it keeps a chunk only when it has bytes), and a flag with
    no chunk behind it hands over nothing at all — neither says the file has none. The walk is
    bounded by the RIFF size, never the physical end of the file: bytes past the container are
    not part of it. Every payload is SEEKED over (odd sizes carry one pad byte); a chunk whose
    header lies inside the container counts even if its size then runs past the end.
    """
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
            return False
        end = 8 + int.from_bytes(head[4:8], "little")
        at = 12
        while at + 8 <= end:
            f.seek(at)
            chunk = f.read(9)
            if len(chunk) < 8:
                return False
            kind = chunk[:4]
            if kind == b"EXIF":
                return True
            if kind == b"VP8X" and at == 12 and len(chunk) == 9:
                if chunk[8] & _VP8X_EXIF_FLAG:
                    return True
            size = int.from_bytes(chunk[4:8], "little")
            at += 8 + size + (size & 1)
        return False


def _png_declares_exif(path: Path) -> bool:
    """Whether a PNG's chunk list, up to `IEND`, holds an `eXIf` chunk — payloads SEEKED over.

    `capture.png_exif` declines a chunk over its read cap and Pillow only sees one placed before
    `IDAT`, so an oversized chunk after the image data reaches this reader as no payload at all.
    Declining to read it is not proof it is absent.
    """
    with open(path, "rb") as f:
        if f.read(len(capture.PNG_SIGNATURE)) != capture.PNG_SIGNATURE:
            return False
        while True:
            header = f.read(8)
            if len(header) < 8 or header[4:8] == b"IEND":
                return False
            if header[4:8] == b"eXIf":
                return True
            f.seek(int.from_bytes(header[:4], "big") + 4, 1)  # payload + CRC, unread


def _declares_exif(path: Path, mime: str, im: Image.Image) -> bool:
    """Whether a file whose reader found no EXIF payload nonetheless declares one."""
    if mime in capture.HEIF_MIMES:
        with open(path, "rb") as f:
            return capture.heif_declares_exif(f)
    if isinstance(im, WebPImagePlugin.WebPImageFile):
        return _webp_declares_exif(path)
    if isinstance(im, PngImagePlugin.PngImageFile):
        return _png_declares_exif(path)
    return False


def _read_image(path: Path, mime: str) -> CameraInfo | None:
    if mime in capture.HEIF_MIMES:
        # BEFORE the open: opening a HEIC parses the whole file.
        with open(path, "rb") as f:
            raw = capture.heif_exif_from(f)
        if raw is not None:
            return _from_payload(raw)
        logger.info("camera.heif_fallback", mime=mime)
    with Image.open(path) as im:
        if isinstance(im, TiffImagePlugin.TiffImageFile):
            # A TIFF's EXIF tags live in the file's own directories: walk them on a fresh handle.
            with open(path, "rb") as f:
                return _from_tiff_structure(f)
        raw = capture.png_exif(path) if isinstance(im, PngImagePlugin.PngImageFile) else None
        if raw is None:
            raw = im.info.get("exif")
        if raw is None:
            if _declares_exif(path, mime, im):
                raise CorruptExif("declared EXIF without a payload")
            return None
        return _from_payload(raw)


def read_camera(path: str | Path, mime: str) -> CameraRead:
    """What `path` says about its camera — the ONE entry point (blocking; run it in a thread).

    Never raises: a failed read is `UNREADABLE`. A non-image is not opened and answers None; its
    ledger value comes from `source_of`, which classifies it by mime.
    """
    if not is_image(mime):
        return None
    try:
        return _read_image(Path(path), mime)
    except Exception as exc:  # noqa: BLE001 — a failed read is an answer, never an exception
        logger.warning("camera.read_failed", mime=mime, error=type(exc).__name__)
        return UNREADABLE
