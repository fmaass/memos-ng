"""Read one file's LOCATION from its own metadata — never from its pixels, and never into a log.

This is the sibling of `services/capture.py` and deliberately a separate module: capture reads a
WHITELIST of date tags and must never touch the GPS IFD (MEMNG-262), so the one place that does
read coordinates is here, where the privacy rules can be stated once and held to.

The rules that make it safe to store a coordinate at all:

* **Metadata only, no decode.** An image's raw EXIF payload is taken exactly the way capture takes
  it — `im.info["exif"]` for JPEG, the ISOBMFF BOX WALKER for HEIF/HEIC, the eXIf CHUNK WALKER for
  PNG, `tag_v2` for TIFF — and parsed with a standalone `Image.Exif`, so no path can fall through
  to `load()`. Asking Pillow for `getexif()` on a PNG whose eXIf chunk sits after IDAT decodes
  every pixel of every photo in the library that has no location; that is the bug this module is
  shaped around.
* **Headers only for video AND for HEIC.** The `moov` box is located by SEEKING over every other
  top-level box (`capture.read_moov_from`), so an 18 GB clip on an NFS mount costs a header read;
  a HEIC's Exif item is found the same way (`capture.heif_exif_from`), because OPENING one parses
  the whole file. `measuring()` meters exactly that, and the backfill reports the total.
* **No value is ever logged.** The one event this module emits is `location.heif_fallback`, which
  carries the mime and nothing else — it says a HEIC's headers could not address its Exif, which
  is a fact about the CONTAINER. A coordinate reaches Postgres and `GET /api/map/markers`, and
  nothing else: not a log line, not a derived tier, not another API body.

Absence is a normal answer and a DIFFERENT fact from failure: `None` means the file was read and
carried no usable location (the caller stores `'none'`), while an unreadable original raises and
the caller stores `'unreadable'`, which a later run retries. Flattening the two would make a
missing NFS mount look like a library of photos that were never geotagged.
"""

import math
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import pillow_heif
import structlog
from PIL import Image, PngImagePlugin, TiffImagePlugin

from app.services import capture

pillow_heif.register_heif_opener()  # HEIC/HEIF primaries (also registered in services.media)

logger = structlog.get_logger()

# The ONE sub-IFD this module reads. Nothing else out of an image reaches a caller.
GPS_IFD = 0x8825
GPS_LATITUDE_REF = 1
GPS_LATITUDE = 2
GPS_LONGITUDE_REF = 3
GPS_LONGITUDE = 4

# The Apple key (moov/meta keys+ilst) and the MP4 spelling (moov/udta).
QUICKTIME_LOCATION_KEY = "com.apple.quicktime.location.ISO6709"
XYZ_BOX = b"\xa9xyz"

# `attachments.location_source`. NULL (absent here) = never read.
SOURCE_EXIF = "exif"
SOURCE_QUICKTIME = "quicktime"
SOURCE_NONE = "none"  # read, and the file carried no usable location
SOURCE_UNREADABLE = "unreadable"  # missing, timed out or unparseable — retryable

MAX_LATITUDE = 90.0
MAX_LONGITUDE = 180.0

# ISO 6709 in its DECIMAL form: ±DD.DDDD±DDD.DDDD[±alt][CRS…][/]. The altitude is a third signed
# number and is deliberately captured-and-dropped rather than left to the longitude's match: a
# pattern that stops after two numbers reads "+40.1-020.2+035.0/" as a longitude of -020.2 and a
# trailing altitude it never notices, which is right by luck and wrong the moment the form changes.
_ISO6709 = re.compile(
    r"^(?P<lat>[+-]\d{1,3}(?:\.\d+)?)"
    r"(?P<lon>[+-]\d{1,3}(?:\.\d+)?)"
    r"(?P<alt>[+-]\d+(?:\.\d+)?)?"
    r"(?:CRS[^/]*)?/?$"
)


class UnreadableOriginal(OSError):
    """The original could not be read at all — as opposed to read and carrying no location.

    An OSError because that is what the rest of this module raises for the same outcome (a missing
    file, an unopenable image), and every caller already treats the two alike.
    """


@dataclass(frozen=True)
class LocationResult:
    """Where one file says it was taken. Degrees, WGS 84, and where the value came from."""

    latitude: float
    longitude: float
    source: str


class ByteMeter:
    """How many bytes the reads under one `measuring()` block actually pulled off the disk.

    The pivot trigger of Phase 1 is read from this: a `moov` at the file TAIL would make the
    backfill's total far exceed the summed header sizes, and the number has to come from the reads
    themselves rather than from an estimate of what the code ought to be doing.
    """

    def __init__(self) -> None:
        self.bytes_read = 0

    def add(self, count: int) -> None:
        self.bytes_read += count


_METER: ContextVar[ByteMeter | None] = ContextVar("location_byte_meter", default=None)


@contextmanager
def measuring() -> Iterator[ByteMeter]:
    """Meter every read `read_location` performs in this context. Nesting replaces, never shares."""
    meter = ByteMeter()
    token = _METER.set(meter)
    try:
        yield meter
    finally:
        _METER.reset(token)


class _MeteredReader:
    """A read-through wrapper that adds what it hands back to the meter. `seek` is NOT counted —
    skipping over `mdat` is exactly the cost this module exists to avoid paying."""

    def __init__(self, inner: BinaryIO, meter: ByteMeter) -> None:
        self._inner = inner
        self._meter = meter

    def read(self, *args: int) -> bytes:
        chunk = self._inner.read(*args)
        self._meter.add(len(chunk))
        return chunk

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@contextmanager
def _opened(path: Path) -> Iterator[BinaryIO]:
    """The file, metered when a `measuring()` block is active and untouched otherwise."""
    with open(path, "rb") as raw:
        meter = _METER.get()
        yield raw if meter is None else cast(BinaryIO, _MeteredReader(raw, meter))


def _finite(raw: object) -> float | None:
    """One EXIF rational as a float, or None for the shapes cameras really emit: a zero
    denominator (NaN), an infinity, a value that is not a number at all."""
    if getattr(raw, "denominator", None) == 0:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]  # the try IS the type check
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) else None


def coordinate_from_dms(values: object, ref: object) -> float | None:
    """`degrees[, minutes[, seconds]]` plus its N/S/E/W ref as signed decimal degrees, or None.

    A missing or empty ref reads as north/east: that is the EXIF default, and refusing the value
    outright would drop every photo from a camera whose firmware omits the tag.
    """
    parts: Sequence[object]
    if isinstance(values, tuple | list):
        parts = values
    else:
        parts = (values,)  # a single rational is a legal, if unusual, spelling of whole degrees
    if not 1 <= len(parts) <= 3:
        return None
    total = 0.0
    for scale, raw in zip((1.0, 60.0, 3600.0), parts, strict=False):
        part = _finite(raw)
        if part is None:
            return None
        total += part / scale
    letter = str(ref).strip()[:1].upper() if isinstance(ref, str) else ""
    return -total if letter in ("S", "W") else total


def location_from_pair(
    latitude: float | None, longitude: float | None, source: str = SOURCE_EXIF
) -> LocationResult | None:
    """The pair as a location, or None when it is not one.

    Exactly (0, 0) is refused. It is a real point in the Gulf of Guinea and it is also what a
    receiver that never got a fix writes, so a library backfill would otherwise grow a pile of
    pins in the Atlantic that no one ever visited.
    """
    if latitude is None or longitude is None:
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if abs(latitude) > MAX_LATITUDE or abs(longitude) > MAX_LONGITUDE:
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    return LocationResult(latitude, longitude, source)


def location_from_gps_ifd(ifd: object) -> LocationResult | None:
    """The GPS IFD's four tags as a location. Takes a mapping so the parse is testable directly."""
    get = getattr(ifd, "get", None)
    if get is None:
        return None
    return location_from_pair(
        coordinate_from_dms(get(GPS_LATITUDE), get(GPS_LATITUDE_REF)),
        coordinate_from_dms(get(GPS_LONGITUDE), get(GPS_LONGITUDE_REF)),
        SOURCE_EXIF,
    )


def parse_iso6709(text: object) -> LocationResult | None:
    """An ISO 6709 decimal string as a location, or None. Altitude and CRS suffix are discarded."""
    if not isinstance(text, str):
        return None
    match = _ISO6709.match(text.strip())
    if match is None:
        return None
    try:
        latitude = float(match.group("lat"))
        longitude = float(match.group("lon"))
    except ValueError:  # pragma: no cover — the pattern already constrains the digits
        return None
    return location_from_pair(latitude, longitude, SOURCE_QUICKTIME)


def _read_image(f: BinaryIO, mime: str) -> LocationResult | None:
    """The image's GPS IFD, from metadata the open already parsed — see the module docstring."""
    if mime in capture.HEIF_MIMES:
        # BEFORE the open: opening a HEIC parses the whole file, which is the cost this read
        # exists to avoid. A payload the headers cannot address falls through to that open, so
        # the answer is never worse than it was — only the cheap case got cheaper.
        raw = capture.heif_exif_from(f)
        if raw is not None:
            exif = Image.Exif()
            exif.load(raw)
            return location_from_gps_ifd(exif.get_ifd(GPS_IFD))
        logger.info("location.heif_fallback", mime=mime)
        f.seek(0)  # the walk left the handle wherever it stopped
    with Image.open(f) as im:
        if isinstance(im, TiffImagePlugin.TiffImageFile):
            exif = im.getexif()  # from tag_v2, parsed at open; no sample decode
        else:
            raw = None
            if isinstance(im, PngImagePlugin.PngImageFile):
                f.seek(0)  # the chunk list, on the handle this call already owns
                raw = capture.png_exif_from(f)
            if raw is None:
                raw = im.info.get("exif")
            if not raw:
                return None
            exif = Image.Exif()
            exif.load(raw)
        # Inside the `with`: a TIFF's Exif resolves the sub-IFD by seeking in the FILE.
        return location_from_gps_ifd(exif.get_ifd(GPS_IFD))


def _iso6709_from_moov(moov: bytes) -> str | None:
    """The location string out of `moov/meta` (Apple) or `moov/udta/©xyz` (MP4), if either is there.

    `meta` is a plain container in QuickTime and a full box (4 leading version/flags bytes) in
    ISO-BMFF; both spellings reach this code from real cameras, so both offsets are tried — the
    same two the capture read walks.
    """
    for kind, start, end in capture.boxes(moov):
        if kind != b"meta":
            continue
        for base in (start, start + 4):
            if base >= end:
                continue
            wanted = capture.keys_index(moov, base, end).get(QUICKTIME_LOCATION_KEY)
            if wanted is None:
                continue
            value = capture.ilst_value(moov, base, end, wanted)
            if value:
                return value
    for kind, start, end in capture.boxes(moov):
        if kind != b"udta":
            continue
        found = capture.child(moov, start, end, XYZ_BOX)
        if found is None:
            continue
        body_start, body_end = found
        # `©xyz` is a QuickTime TEXT atom, not the `data` sub-box the keys/ilst path carries:
        # a 2-byte character count, a 2-byte language code, then the string itself.
        if body_start + 4 > body_end:
            continue
        count = int.from_bytes(moov[body_start : body_start + 2], "big")
        text = moov[body_start + 4 : min(body_start + 4 + count, body_end)]
        if text:
            return text.decode("utf-8", "replace")
    return None


def _read_video(f: BinaryIO) -> LocationResult | None:
    moov = capture.read_moov_from(f)
    if moov is None:
        # No moov box, a truncated one, or bytes that are no container at all. Every mp4/mov that
        # can be played has one, so this is damage, not silence — and the two settle differently:
        # see `read_location`. Answering None here would write the file off as 'none' forever.
        raise UnreadableOriginal("no readable moov box")
    return parse_iso6709(_iso6709_from_moov(moov))


def read_location(path: str | Path, mime: str) -> LocationResult | None:
    """Where `path` says it was taken — the ONE entry point (blocking; call it in a worker thread).

    Answers None when the file was read and carried no usable location. RAISES when the file could
    not be read at all: the caller turns that into `'unreadable'`, which a later run retries, and
    the distinction is the difference between "no one geotagged these" and "the mount was down".
    """
    target = Path(path)
    if mime.startswith("image/"):
        with _opened(target) as f:
            return _read_image(f, mime)
    if mime.startswith("video/"):
        with _opened(target) as f:
            return _read_video(f)
    return None
