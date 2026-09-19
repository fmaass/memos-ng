"""Read one file's CAPTURE date from its metadata — never from its pixels (MEMNG-262).

The rules live in `fixtures/auto-memo-date.json`, the artefact this module, the SPA and the iOS
client are all held to; the vectors there drive `tests/test_capture.py`. Two things make this
module different from the EXIF code already in `services/media.py`:

* **Metadata only.** An image is opened lazily and only its EXIF header is read — no `.load()`,
  no decode. A video is walked atom by atom with `seek`, so the `mdat` (the samples, i.e. the
  whole file for a 4K clip) is never read: only the `moov` box reaches memory, and only up to
  `MOOV_READ_CAP`. A HEIC is walked the same way (`heif_exif_from`), because opening one costs
  the WHOLE file — pillow-heif parses the image payload to open it, and only the `meta` box and
  the Exif item's own extent are wanted, capped by `HEIF_EXIF_READ_CAP`.
* **A date-tag WHITELIST.** Exactly three EXIF tags and one QuickTime key are read. GPS and
  everything else is neither read nor logged, and no failure ever carries metadata content into a
  log line — only the exception's type.

Absence is the normal answer: anything unreadable, malformed or absent yields `NOTHING`, never an
exception to the caller. A capture date the app cannot prove is worth less than no date at all.
"""

import logging
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

import pillow_heif
import structlog
from PIL import Image, PngImagePlugin, TiffImagePlugin

pillow_heif.register_heif_opener()  # HEIC/HEIF primaries (also registered in services.media)

logger = structlog.get_logger()

# ONCE, at import, and never restored: PIL's TIFF/PNG plugins log every tag they walk at DEBUG —
# including the ones this module deliberately does not read — so a `logging.basicConfig(DEBUG)`
# anywhere in the process would put GPS coordinates in the log file. It is set here rather than
# around each read because the level is process-wide state: two reads on two threads would
# interleave a save/restore and leave the loggers wherever the loser put them. Nothing in this
# application ever wants PIL's debug output; when a PIL problem needs debugging, it is debugged by
# raising it here on purpose.
for _pil_logger in ("PIL", "PIL.Image", "PIL.TiffImagePlugin", "PIL.PngImagePlugin"):
    logging.getLogger(_pil_logger).setLevel(logging.INFO)

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
# A PNG's eXIf chunk is metadata; the same reasoning (and the same generosity) as the moov cap.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_EXIF_READ_CAP = 2 * 1024 * 1024
# A HEIF's `meta` box and its Exif item are metadata too, and the cap bounds BOTH: the box this
# walk parses in memory, and the one extent it then reads out of the file.
HEIF_EXIF_READ_CAP = 2 * 1024 * 1024
# The mimes `uploads.detect_heif_mime` puts on an ISOBMFF still. AVIF is one of them: the walk
# reads `meta` → `iinf`/`iloc` and never a codec, so the AV1 payload it skips is as opaque to it
# as the HEVC one — and `fixtures/locations/avif-gps.avif` is what holds that claim to a file,
# because a wrong Exif item would be a wrong DATE rather than an error.
HEIF_MIMES = frozenset(
    {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence", "image/avif"}
)
EXIF_PREFIX = b"Exif\x00\x00"
TIFF_MAGIC = (b"II*\x00", b"MM\x00*")

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
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
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


def png_exif(path: Path) -> bytes | None:
    """The bytes of a PNG's `eXIf` chunk, found by walking the chunk list — or None.

    Pillow stops parsing a PNG's header at the first `IDAT`, so `im.info["exif"]` only ever holds
    an eXIf chunk that happens to precede the image data. Both placements are legal and both occur
    (`fixtures/auto-memo-date/png-exif-after-idat.png` is the other one), so the list is walked
    here: each chunk's 8-byte length+type header is read and its payload SEEKED over — `IDAT` is
    never read, only skipped — and only an `eXIf` payload is ever pulled into memory.
    """
    with open(path, "rb") as f:
        return png_exif_from(f)


def png_exif_from(f: BinaryIO) -> bytes | None:
    """`png_exif` against an ALREADY-OPEN file — the spelling a caller that meters its own reads
    (services/location.py) needs, since the whole point is that it supplies the file object."""
    if f.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
        return None
    while True:
        header = f.read(8)
        if len(header) < 8:
            return None
        length = int.from_bytes(header[:4], "big")
        kind = header[4:8]
        if kind == b"IEND":
            return None
        if kind == b"eXIf":
            if length > PNG_EXIF_READ_CAP:
                return None
            payload = f.read(length)
            # The same shape the other containers hand over, so one parser reads them all.
            return b"Exif\x00\x00" + payload
        f.seek(length + 4, 1)  # payload + CRC, unread


def heif_exif_from(f: BinaryIO) -> bytes | None:
    """A HEIF/HEIC's Exif payload, found by walking its boxes and reading ONE extent — or None.

    pillow-heif parses the WHOLE file to open it, image payload included, so asking Pillow for
    `im.info["exif"]` costs a 4 MB read for four kilobytes of metadata — over NFS that is what put
    the location backfill and the upload's 5 s read budget at the mercy of the mount. The box walk
    is the same trick `read_moov_from` plays on a video: every box but `meta` is SEEKED over,
    `mdat` is never read, and only the Exif item's own extent reaches memory.

    None means "not answerable from the headers", never "no Exif": an item addressed inside `idat`
    or relative to another item, an extent in a second file, a split extent, a cap overrun, a
    truncated or hostile header. The caller falls back to the full read, which is slower and
    always right — correctness is not the thing being traded away here.
    """
    meta = _heif_meta_from(f)
    if meta is None:
        return None
    children = _heif_children(meta)
    if children is None:
        return None
    item = _heif_exif_item(meta, children)
    if item is None:
        return None
    extent = _heif_extent(meta, children, item)
    if extent is None:
        return None
    offset, length = extent
    if not 4 < length <= HEIF_EXIF_READ_CAP:
        return None
    f.seek(offset)
    raw = f.read(length)
    if len(raw) != length:  # a truncated file, and a truncated Exif parses into plausible noise
        return None
    return _heif_exif_payload(raw)


def _heif_meta_from(f: BinaryIO) -> bytes | None:
    """The `meta` box's body, found by SEEKING over every other top-level box.

    The same walk as `read_moov_from`, for the box a still image keeps its item table in.

    The first box must be `ftyp`: it is what says this is an ISOBMFF file at all, and refusing
    anything else is what keeps a JPEG or a stream of noise from being walked as one.
    """
    first = True
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
        if first and kind != b"ftyp":
            return None
        first = False
        if kind == b"meta":
            payload_size = size - header
            if size == 0 or payload_size <= 0 or payload_size > HEIF_EXIF_READ_CAP:
                return None
            payload = f.read(payload_size)
            return payload if len(payload) == payload_size else None
        if size == 0 or size < header:  # "to end of file", or nonsense — nothing follows
            return None
        f.seek(size - header, 1)


def _heif_boxes(meta: bytes, start: int, end: int) -> list[tuple[bytes, int, int]] | None:
    """The boxes in `meta[start:end]` — but only if they TILE it exactly, else None.

    `boxes` stops at the first length it cannot believe, which is right for a walk that wants
    whatever it can get. Here the opposite is wanted: a child running past its parent, or a gap
    after the last one, means the table is partly guesswork, and half a malformed item list is
    exactly what a reader must not answer from.
    """
    found = list(boxes(meta, start, end))
    covered = found[-1][2] if found else start
    return found if covered == end else None


def _heif_children(meta: bytes) -> dict[bytes, tuple[int, int]] | None:
    """`meta`'s child boxes by type, once the box itself can be trusted — else None.

    `meta` is a FullBox and version 0 is the only one anyone has defined; a later one would be a
    layout this code has never seen, not a detail to read past. The `pict` handler is the check
    that this `meta` describes IMAGE ITEMS — a `meta` full of iTunes keys (the shape the moov walk
    beside this one reads) has an `iloc` in no sense at all, and reading one as if it did is how a
    walker invents an offset.
    """
    if len(meta) < 4 or meta[0] != 0:
        return None
    found = _heif_boxes(meta, 4, len(meta))
    if found is None:
        return None
    children: dict[bytes, tuple[int, int]] = {}
    for kind, body, box_end in found:
        children.setdefault(kind, (body, box_end))  # a duplicate box is the first one's problem
    hdlr = children.get(b"hdlr")
    if hdlr is None:
        return None
    body, end = hdlr
    if body + 12 > end or meta[body] != 0 or meta[body + 8 : body + 12] != b"pict":
        return None
    return children


def _heif_item_types(
    meta: bytes, children: dict[bytes, tuple[int, int]]
) -> dict[int, bytes] | None:
    """Every item's four-character type, out of `iinf`'s `infe` entries — or None.

    The entry count is checked against the entries actually there, in both directions: a count
    that overstates them leaves a table missing the very item the read wants, and a count that
    understates them leaves entries nobody validated. Versions 0 and 1 of `infe` name items
    without saying what they are, so a file built from them is answered by the full read rather
    than by a guess about which unnamed item holds the Exif.
    """
    iinf = children.get(b"iinf")
    if iinf is None:
        return None
    body, end = iinf
    if body + 4 > end or meta[body] > 1:
        return None
    count_size = 2 if meta[body] == 0 else 4
    if body + 4 + count_size > end:
        return None
    declared = int.from_bytes(meta[body + 4 : body + 4 + count_size], "big")
    entries = _heif_boxes(meta, body + 4 + count_size, end)
    if entries is None or len(entries) != declared:
        return None
    types: dict[int, bytes] = {}
    for kind, info, info_end in entries:
        if kind != b"infe" or info >= info_end:
            return None
        version = meta[info]
        if version == 2:
            id_size, type_at = 2, 8
        elif version == 3:
            id_size, type_at = 4, 10
        else:
            return None
        if info + type_at + 4 > info_end:
            return None
        item = int.from_bytes(meta[info + 4 : info + 4 + id_size], "big")
        types[item] = meta[info + type_at : info + type_at + 4]
    if len(types) != len(entries):  # two entries claiming one ID: which is which is unknowable
        return None
    return types


def _heif_primary_item(meta: bytes, children: dict[bytes, tuple[int, int]]) -> int | None:
    """`pitm`'s item ID — WHICH image the file is, when it carries more than one."""
    pitm = children.get(b"pitm")
    if pitm is None:
        return None
    body, end = pitm
    if body >= end or meta[body] > 1:
        return None
    width = 2 if meta[body] == 0 else 4
    if body + 4 + width > end:
        return None
    return int.from_bytes(meta[body + 4 : body + 4 + width], "big")


def _heif_exif_item(meta: bytes, children: dict[bytes, tuple[int, int]]) -> int | None:
    """The item ID of the Exif that describes the PRIMARY image — or None.

    Which Exif belongs to which image is stated ONLY by the `cdsc` reference in `iref`, and that
    holds however many Exif items the file carries: a HEIC with two images and ONE Exif item is a
    file whose single Exif may well describe the secondary (`heic-offset.heic` becomes exactly
    that when the primary's entry is retyped, and libheif then reports no Exif for the primary at
    all). Treating "only one" as "the primary's" dates the memo from the wrong picture, and bytes
    returned are past every fallback there is — so the reference is required, never inferred.
    """
    types = _heif_item_types(meta, children)
    if types is None:
        return None
    items = [item for item, kind in types.items() if kind == b"Exif"]
    primary = _heif_primary_item(meta, children)
    iref = children.get(b"iref")
    if not items or primary is None or iref is None:
        return None
    body, end = iref
    if body + 4 > end or meta[body] > 1:
        return None
    width = 2 if meta[body] == 0 else 4
    references = _heif_boxes(meta, body + 4, end)
    if references is None:
        return None
    # The WHOLE table is validated before any match in it is believed. A reference that matches
    # says nothing about the ones after it, and a file whose second reference over-counts is a
    # file libheif refuses outright — answering from the good half of it would turn an unreadable
    # original into a successful read and skip the fallback that would have raised.
    found: int | None = None
    for kind, ref, ref_end in references:
        if ref + width + 2 > ref_end:
            return None
        source = int.from_bytes(meta[ref : ref + width], "big")
        count = int.from_bytes(meta[ref + width : ref + width + 2], "big")
        if ref + width + 2 + count * width != ref_end:  # the count and the bytes must agree
            return None
        if kind != b"cdsc" or source not in items:
            continue
        for index in range(count):
            at = ref + width + 2 + index * width
            if int.from_bytes(meta[at : at + width], "big") == primary:
                if found is not None and found != source:
                    return None  # two Exif items claiming the same image: unknowable
                found = source
    return found


def _heif_extent(
    meta: bytes, children: dict[bytes, tuple[int, int]], item: int
) -> tuple[int, int] | None:
    """Where one item's bytes are, as a (file offset, length) — or None when they are not there.

    `iloc` is the one box in this walk whose field WIDTHS are themselves data: offsets, lengths,
    base offsets and extent indices are each 0, 4 or 8 bytes wide as the header says, and the item
    IDs and the count widen again in version 2. Reading a later version with the earlier widths
    lands mid-field and yields an offset that is pure fiction, which is why an unknown version,
    an unknown width or a construction_method other than 0 (the payload is in `idat`, or inside
    another item) answers None instead of a number.
    """
    iloc = children.get(b"iloc")
    if iloc is None:
        return None
    body, body_end = iloc
    if body + 6 > body_end:
        return None
    version = meta[body]
    if version > 2:
        return None
    offset_size, length_size = meta[body + 4] >> 4, meta[body + 4] & 0xF
    base_size = meta[body + 5] >> 4
    index_size = (meta[body + 5] & 0xF) if version in (1, 2) else 0
    if any(width not in (0, 4, 8) for width in (offset_size, length_size, base_size, index_size)):
        return None
    id_size = 4 if version == 2 else 2
    cursor = body + 6
    count_size = 4 if version == 2 else 2
    if cursor + count_size > body_end:
        return None
    count = int.from_bytes(meta[cursor : cursor + count_size], "big")
    cursor += count_size
    # As in `_heif_exif_item`: the item table is walked to its END before the row this read wants
    # is believed. An `iloc` declaring more items than it writes is a corrupt table whichever side
    # of the match the shortfall falls on, and only a complete walk can tell.
    found: tuple[int, int] | None = None
    for _ in range(count):
        head = id_size + (2 if version in (1, 2) else 0) + 2 + base_size + 2
        if cursor + head > body_end:
            return None
        current = int.from_bytes(meta[cursor : cursor + id_size], "big")
        cursor += id_size
        method = 0
        if version in (1, 2):
            method = meta[cursor + 1] & 0x0F
            cursor += 2
        reference = int.from_bytes(meta[cursor : cursor + 2], "big")
        cursor += 2
        base = int.from_bytes(meta[cursor : cursor + base_size], "big") if base_size else 0
        cursor += base_size
        extents = int.from_bytes(meta[cursor : cursor + 2], "big")
        cursor += 2
        span = index_size + offset_size + length_size
        if cursor + extents * span > body_end:
            return None
        if current != item:
            cursor += extents * span
            continue
        # construction_method 0 is a FILE offset; a data_reference_index names ANOTHER file; and a
        # split extent would have to be stitched. None of the three is a header read of this file.
        if method != 0 or reference != 0 or extents != 1 or found is not None:
            return None
        # The index is SKIPPED, and the length is measured from the end of the OFFSET — not from
        # the end of the whole extent, which would read `index_size` bytes too far and turn a
        # declared 32 into billions.
        offset_at = cursor + index_size
        offset = (
            int.from_bytes(meta[offset_at : offset_at + offset_size], "big") if offset_size else 0
        )
        length_at = offset_at + offset_size
        length = (
            int.from_bytes(meta[length_at : length_at + length_size], "big") if length_size else 0
        )
        found = (base + offset, length)
        cursor += extents * span
    if cursor != body_end:  # the rows must account for the box exactly, with nothing left over
        return None
    return found


def _heif_exif_payload(raw: bytes) -> bytes | None:
    """One Exif item's extent as the payload a parser takes, or None if it is not one.

    The extent is an ExifDataBlock: a 4-byte `exif_tiff_header_offset` and then the payload. The
    four bytes are dropped rather than followed, because that is byte-for-byte what pillow-heif
    hands over in `im.info["exif"]` — the whole point is that the two readers are interchangeable.
    A payload that is neither APP1-prefixed nor a TIFF header is refused: an offset read from a
    file that turned out not to be shaped as assumed must never be handed on as metadata.
    """
    payload = raw[4:]
    if payload.startswith(EXIF_PREFIX) or payload[:4] in TIFF_MAGIC:
        return payload
    skip = int.from_bytes(raw[:4], "big")
    if 8 + skip <= len(raw) and raw[4 + skip : 8 + skip] in TIFF_MAGIC:
        return raw[4 + skip :]
    return None


def _whitelisted_tags(exif: Image.Exif) -> CaptureResult:
    """The three date tags out of an already-parsed Exif, and nothing else.

    The whitelist is enforced by asking for ONE sub-IFD: `get_ifd(0x8769)` parses the Exif IFD and
    nothing else, so the GPS IFD (0x8825) is never even walked. Parsing that IFD in memory and
    reading three tags out of it — then discarding the rest with the object — IS what the
    whitelist means: no other tag reaches a caller, a log line, or the database.
    """
    ifd = exif.get_ifd(EXIF_IFD)
    offset = ifd.get(OFFSET_TIME_ORIGINAL)
    if offset is None:
        offset = ifd.get(OFFSET_TIME)
    return capture_from_exif(ifd.get(DATETIME_ORIGINAL), offset)


def _read_image(path: Path, mime: str) -> CaptureResult:
    """The PRIMARY image's whitelisted EXIF tags, read from metadata the OPEN already parsed.

    Two containers, two places that metadata lives:

    * JPEG — `im.info["exif"]` is the raw APP1 payload the plugin picked up while parsing the
      header.
    * HEIF / HEIC — the box walk above (`heif_exif_from`), because an OPEN costs the whole file
      here: pillow-heif parses the image payload too. It reads the PRIMARY item's Exif, which is
      NOT necessarily the first one (`fixtures/auto-memo-date/heic-offset.heic` carries a
      different date on its first item on purpose). When the walk declines, the open is the
      fallback and answers exactly as it always did.
    * PNG — the chunk list is walked here (`_png_exif`), because Pillow stops at IDAT and an eXIf
      chunk is equally legal on the far side of it.
    * TIFF — there is no such payload: the EXIF tags live in the file's own first IFD, which
      `TiffImageFile` parses at open into `tag_v2`. `getexif()` reads THAT, so it costs nothing and
      decodes nothing here — while for a PNG with no eXIf chunk the same call would fall through
      to `load()` and decode the pixels of every photo that has no date, which is the one thing
      this module promises never to do.

    The whitelist itself is `_whitelisted_tags`, which both paths end in.
    """
    if mime in HEIF_MIMES:
        with open(path, "rb") as f:
            raw = heif_exif_from(f)
        if raw is not None:
            exif = Image.Exif()
            exif.load(raw)
            return _whitelisted_tags(exif)
        # The mime and nothing else: this is the one HEIC in a thousand whose Exif the headers
        # cannot address, and knowing how often that happens is worth a line. A value out of the
        # metadata never is.
        logger.info("capture.heif_fallback", mime=mime)
    with Image.open(path) as im:
        if isinstance(im, TiffImagePlugin.TiffImageFile):
            exif = im.getexif()  # from tag_v2, parsed at open; no sample decode
        else:
            raw = png_exif(path) if isinstance(im, PngImagePlugin.PngImageFile) else None
            if raw is None:
                raw = im.info.get("exif")
            if not raw:
                return NOTHING
            exif = Image.Exif()
            exif.load(raw)
        # Inside the `with`: a TIFF's Exif object resolves the sub-IFD by seeking in the FILE, so
        # the handle has to still be open. (A standalone Exif built from bytes does not care.)
        return _whitelisted_tags(exif)


def boxes(buf: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
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


def child(buf: bytes, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for found, body_start, body_end in boxes(buf, start, end):
        if found == kind:
            return body_start, body_end
    return None


def keys_index(buf: bytes, start: int, end: int) -> dict[str, int]:
    """The `keys` atom's key → 1-based index map (the index an `ilst` child is named by)."""
    keys = child(buf, start, end, b"keys")
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


def ilst_value(buf: bytes, start: int, end: int, wanted: int) -> str | None:
    """The UTF-8 payload of the `ilst` entry named by `wanted` (its `keys` index)."""
    ilst = child(buf, start, end, b"ilst")
    if ilst is None:
        return None
    for kind, body_start, body_end in boxes(buf, *ilst):
        if int.from_bytes(kind, "big") != wanted:
            continue
        data = child(buf, body_start, body_end, b"data")
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
    for kind, start, end in boxes(moov):
        if kind != b"meta":
            continue
        for base in (start, start + 4):
            if base >= end:
                continue
            wanted = keys_index(moov, base, end).get(QUICKTIME_CREATIONDATE_KEY)
            if wanted is None:
                continue
            value = ilst_value(moov, base, end, wanted)
            if value:
                return value
    return None


def _movie_header_seconds(moov: bytes) -> int | None:
    """`mvhd`'s creation_time, in its own 1904 epoch. Track headers are deliberately not read."""
    mvhd = child(moov, 0, len(moov), b"mvhd")
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


def read_moov(path: Path) -> bytes | None:
    """The `moov` box's body, found by SEEKING over every other top-level box.

    This is the whole reason a video costs no more than a photo here: `mdat` — which is the file,
    for anything longer than a second — is skipped with `seek`, never read. Only `moov` is read,
    and only up to `MOOV_READ_CAP`.
    """
    with open(path, "rb") as f:
        return read_moov_from(f)


def read_moov_from(f: BinaryIO) -> bytes | None:
    """`read_moov` against an ALREADY-OPEN file — see `png_exif_from`."""
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
            payload = f.read(body)
            # A short read is a TRUNCATED file, and a truncated moov parses into a plausible but
            # arbitrary answer. Reported as "no moov" so a caller can tell damage from silence.
            return payload if len(payload) == body else None
        if size == 0 or size < header:  # "to end of file", or nonsense — nothing follows
            return None
        f.seek(size - header, 1)


def _read_video(path: Path) -> CaptureResult:
    moov = read_moov(path)
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
            return _read_image(Path(path), mime)
        if mime.startswith("video/"):
            return _read_video(Path(path))
    except Exception as exc:  # noqa: BLE001 — a metadata read must never fail an upload
        logger.warning("capture.read_failed", mime=mime, error=type(exc).__name__)
    return NOTHING
