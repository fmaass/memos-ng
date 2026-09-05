"""Read one file's CAPTURE date from its metadata — never from its pixels (MEMNG-262).

The rules live in `fixtures/auto-memo-date.json`, the artefact this module, the SPA and the iOS
client are all held to; the vectors there drive `tests/test_capture.py`. Two things make this
module different from the EXIF code already in `services/media.py`:

* **Metadata only.** An image is opened lazily and only its EXIF header is read — no `.load()`,
  no decode. A video is walked atom by atom with `seek`, so the `mdat` (the samples, i.e. the
  whole file for a 4K clip) is never read: only the `moov` box reaches memory, and only up to
  `MOOV_READ_CAP`.
* **A date-tag WHITELIST.** Exactly three EXIF tags and one QuickTime key are read. GPS and
  everything else is neither read nor logged, and no failure ever carries metadata content into a
  log line — only the exception's type.

Absence is the normal answer: anything unreadable, malformed or absent yields `NOTHING`, never an
exception to the caller. A capture date the app cannot prove is worth less than no date at all.
"""

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pillow_heif
import structlog
from PIL import Image

pillow_heif.register_heif_opener()  # HEIC/HEIF primaries (also registered in services.media)

logger = structlog.get_logger()

# The whole whitelist. Nothing else is read out of an image.
DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011
OFFSET_TIME = 0x9010
EXIF_IFD = 0x8769

QUICKTIME_CREATIONDATE_KEY = "com.apple.quicktime.creationdate"

# QuickTime counts from 1904-01-01 UTC; 2082844800 s of that lie before the Unix epoch.
QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
# The movie header is a FALLBACK, and an unset one reads as 1904 or as a stray small number. Only
# a header at or after this floor is believed; a genuinely old PHOTOGRAPH is unaffected, because
# the floor applies to the video header alone (an EXIF date from 1975 stays valid).
MIN_HEADER_INSTANT = datetime(1990, 1, 1, tzinfo=UTC)
MAX_HEADER_SKEW = timedelta(days=1)
# A `moov` box is metadata; a megabyte of it is already extravagant. The cap bounds what a hostile
# or corrupt header can make this process allocate.
MOOV_READ_CAP = 8 * 1024 * 1024

_EXIF_DATETIME = "%Y:%m:%d %H:%M:%S"
_OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
_ISO_BASIC_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


@dataclass(frozen=True)
class CaptureResult:
    """What one file's metadata says about when it was captured.

    Exactly one of `capture_instant` / `capture_civil` is ever set: an instant is a point in time
    everyone agrees on, a civil time is a wall-clock reading whose zone the file did not record —
    only the CLIENT knows the zone to resolve it in, so the server hands it over unresolved.
    """

    capture_instant: datetime | None = None
    capture_civil: str | None = None
    capture_source: str | None = None


NOTHING = CaptureResult()


def parse_exif_datetime(raw: object) -> str | None:
    """EXIF `YYYY:MM:DD HH:MM:SS` as an ISO 8601 CIVIL time, or None.

    The common "unset" spellings — `0000:00:00 00:00:00`, blanks, NUL padding — fail the parse and
    so answer None, which is what makes them ignorable rather than a memo dated in year zero.
    """
    if not isinstance(raw, str):
        return None
    text = raw.replace("\x00", "").strip()
    try:
        dt = datetime.strptime(text, _EXIF_DATETIME)
    except ValueError:
        return None
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )


def parse_exif_offset(raw: object) -> timedelta | None:
    """EXIF `+HH:MM` / `-HH:MM` as an offset, or None (which demotes the read to a civil time)."""
    if not isinstance(raw, str):
        return None
    m = _OFFSET.match(raw.replace("\x00", "").strip())
    if m is None:
        return None
    sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3))
    if hours > 23 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return -delta if sign == "-" else delta


def capture_from_exif(datetime_original: object, offset: object) -> CaptureResult:
    """The whitelist's two values as a capture result — the pure half of the image read."""
    civil = parse_exif_datetime(datetime_original)
    if civil is None:
        return NOTHING
    delta = parse_exif_offset(offset)
    if delta is None:
        # No offset (or an unreadable one): the wall-clock reading is all the file knows, and
        # resolving it here would mean guessing a zone. The client resolves it in its own.
        return CaptureResult(None, civil, "exif")
    instant = datetime.fromisoformat(civil).replace(tzinfo=timezone(delta)).astimezone(UTC)
    return CaptureResult(instant, None, "exif")


def parse_quicktime_creationdate(text: object) -> datetime | None:
    """`com.apple.quicktime.creationdate` (ISO 8601 WITH an offset) as a UTC instant, or None.

    A value without an offset is refused rather than assumed to be UTC: the key's contract is a
    zoned stamp, the movie header below is the defined fallback, and guessing here would put the
    two clients (which parse it with strict ISO 8601 readers) at odds with this one. Sub-second
    digits are floored — precision is whole seconds on every wire in this feature.
    """
    if not isinstance(text, str):
        return None
    value = _ISO_BASIC_OFFSET.sub(r"\1:\2", text.strip())
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).replace(microsecond=0)


def instant_from_movie_header(seconds: int, now: datetime | None = None) -> datetime | None:
    """The `mvhd` creation_time (seconds since 1904-01-01 UTC) as an instant, or None.

    Rejected when zero (the unset header every muxer writes when it has no date), before the 1990
    floor, or more than a day into the future — a clock that wrong is not evidence of anything.
    """
    if seconds <= 0:
        return None
    try:
        instant = QUICKTIME_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None
    if instant < MIN_HEADER_INSTANT:
        return None
    if instant > (now or datetime.now(UTC)) + MAX_HEADER_SKEW:
        return None
    return instant.replace(microsecond=0)


def _read_image(path: Path) -> CaptureResult:
    """The PRIMARY image's whitelisted EXIF tags. Pillow opens lazily; pixels are never decoded.

    pillow_heif answers for the primary item of a multi-image HEIC — which is NOT necessarily the
    first one, as `fixtures/auto-memo-date/heic-offset.heic` proves (its first item carries a
    different date on purpose).
    """
    with Image.open(path) as im:
        ifd = im.getexif().get_ifd(EXIF_IFD)
    offset = ifd.get(OFFSET_TIME_ORIGINAL)
    if offset is None:
        offset = ifd.get(OFFSET_TIME)
    return capture_from_exif(ifd.get(DATETIME_ORIGINAL), offset)


def _boxes(buf: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    """Walk the ISO-BMFF boxes in `buf[start:end]`, yielding (type, body_start, body_end).

    Stops at the first malformed length rather than raising: a truncated or hostile header ends
    the walk, it does not end the upload.
    """
    limit = len(buf) if end is None else end
    i = start
    while i + 8 <= limit:
        (size,) = struct.unpack_from(">I", buf, i)
        kind = buf[i + 4 : i + 8]
        header = 8
        if size == 1:
            if i + 16 > limit:
                return
            (size,) = struct.unpack_from(">Q", buf, i + 8)
            header = 16
        elif size == 0:
            size = limit - i
        if size < header or i + size > limit:
            return
        yield kind, i + header, i + size
        i += size


def _child(buf: bytes, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for found, body_start, body_end in _boxes(buf, start, end):
        if found == kind:
            return body_start, body_end
    return None


def _keys_index(buf: bytes, start: int, end: int) -> dict[str, int]:
    """The `keys` atom's key → 1-based index map (the index an `ilst` child is named by)."""
    keys = _child(buf, start, end, b"keys")
    if keys is None:
        return {}
    body_start, body_end = keys
    i = body_start + 8  # version+flags, entry_count
    index: dict[str, int] = {}
    n = 0
    while i + 8 <= body_end:
        (size,) = struct.unpack_from(">I", buf, i)
        if size < 8 or i + size > body_end:
            break
        n += 1
        index[buf[i + 8 : i + size].decode("utf-8", "replace")] = n
        i += size
    return index


def _ilst_value(buf: bytes, start: int, end: int, wanted: int) -> str | None:
    """The UTF-8 payload of the `ilst` entry named by `wanted` (its `keys` index)."""
    ilst = _child(buf, start, end, b"ilst")
    if ilst is None:
        return None
    for kind, body_start, body_end in _boxes(buf, *ilst):
        if int.from_bytes(kind, "big") != wanted:
            continue
        data = _child(buf, body_start, body_end, b"data")
        if data is None:
            return None
        payload_start, payload_end = data
        return buf[payload_start + 8 : payload_end].decode("utf-8", "replace")
    return None


def _creationdate_from_moov(moov: bytes) -> str | None:
    """`com.apple.quicktime.creationdate` out of `moov/meta` (keys + ilst), if it is there.

    `meta` is a plain container in QuickTime and a full box (4 leading version/flags bytes) in
    ISO-BMFF, and both spellings reach this code from real cameras, so both offsets are tried.
    """
    for kind, start, end in _boxes(moov):
        if kind != b"meta":
            continue
        for base in (start, start + 4):
            if base >= end:
                continue
            wanted = _keys_index(moov, base, end).get(QUICKTIME_CREATIONDATE_KEY)
            if wanted is None:
                continue
            value = _ilst_value(moov, base, end, wanted)
            if value:
                return value
    return None


def _movie_header_seconds(moov: bytes) -> int | None:
    """`mvhd`'s creation_time, in its own 1904 epoch. Track headers are deliberately not read."""
    mvhd = _child(moov, 0, len(moov), b"mvhd")
    if mvhd is None:
        return None
    start, end = mvhd
    version = moov[start]
    if version == 1:
        if start + 12 > end:
            return None
        return int.from_bytes(moov[start + 4 : start + 12], "big")
    if start + 8 > end:
        return None
    return int.from_bytes(moov[start + 4 : start + 8], "big")


def _read_moov(path: Path) -> bytes | None:
    """The `moov` box's body, found by SEEKING over every other top-level box.

    This is the whole reason a video costs no more than a photo here: `mdat` — which is the file,
    for anything longer than a second — is skipped with `seek`, never read. Only `moov` is read,
    and only up to `MOOV_READ_CAP`.
    """
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return None
            (size,) = struct.unpack_from(">I", head, 0)
            kind = head[4:8]
            header = 8
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    return None
                (size,) = struct.unpack_from(">Q", ext, 0)
                header = 16
            if kind == b"moov":
                if size == 0:
                    return f.read(MOOV_READ_CAP + 1)[:MOOV_READ_CAP]
                body = size - header
                if body <= 0 or body > MOOV_READ_CAP:
                    return None
                return f.read(body)
            if size == 0 or size < header:  # "to end of file", or nonsense — nothing follows
                return None
            f.seek(size - header, 1)


def _read_video(path: Path) -> CaptureResult:
    moov = _read_moov(path)
    if moov is None:
        return NOTHING
    instant = parse_quicktime_creationdate(_creationdate_from_moov(moov))
    if instant is not None:
        return CaptureResult(instant, None, "quicktime")
    seconds = _movie_header_seconds(moov)
    if seconds is None:
        return NOTHING
    instant = instant_from_movie_header(seconds)
    if instant is None:
        return NOTHING
    return CaptureResult(instant, None, "quicktime")


def read_capture(path: str | Path, mime: str) -> CaptureResult:
    """When `path` was captured, per its own metadata — the ONE entry point (blocking; call it in
    a worker thread).

    Never raises: an unreadable, truncated or hostile file is a file with no capture date. The
    warning carries the mime and the exception TYPE only — never a value out of the metadata.
    """
    try:
        if mime.startswith("image/"):
            return _read_image(Path(path))
        if mime.startswith("video/"):
            return _read_video(Path(path))
    except Exception as exc:  # noqa: BLE001 — a metadata read must never fail an upload
        logger.warning("capture.read_failed", mime=mime, error=type(exc).__name__)
    return NOTHING
