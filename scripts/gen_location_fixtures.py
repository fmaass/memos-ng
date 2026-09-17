"""Author the twelve binary fixtures behind `fixtures/locations.json`.

Run it only to (re)create them; the committed files are the contract, and their sha256 is
recorded in the artefact so a silent edit fails the suite. exiftool and ffmpeg are used HERE, at
authoring time, on a workstation — the app never depends on either for reading a location
(`app/services/location.py` is Pillow plus the pure-Python moov walker of `services/capture.py`).

    .venv/bin/python scripts/gen_location_fixtures.py

EVERY COORDINATE IN THIS FILE IS FICTIONAL. They are invented points in open ocean, chosen so
that no fixture can ever disclose a household location — the whole reason the feature stores
coordinates at all is that they are private, and a test fixture is the one place they would
otherwise leak into a public repository.

Each file is deliberately tiny (8x8 stills, 0.5 s 32x32 clips) and carries exactly one
interesting fact, so a failure names a rule rather than a file.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pillow_heif
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "locations"

GPS_IFD = 0x8825
GPS_LATITUDE_REF = 1
GPS_LATITUDE = 2
GPS_LONGITUDE_REF = 3
GPS_LONGITUDE = 4

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _gps_exif(
    lat: tuple[int, int, int] | None = None,
    lat_ref: str | None = None,
    lon: tuple[int, int, int] | None = None,
    lon_ref: str | None = None,
    *,
    denominator: int = 1,
) -> bytes:
    """A raw EXIF payload carrying a GPS IFD and nothing else.

    `denominator` is a knob rather than a constant so the zero-denominator fixture — a real
    camera-firmware bug, and a division by zero in a naive reader — can be authored at all.
    """
    exif = Image.Exif()
    ifd = exif.get_ifd(GPS_IFD)
    if lat_ref is not None:
        ifd[GPS_LATITUDE_REF] = lat_ref
    if lat is not None:
        ifd[GPS_LATITUDE] = tuple(IFDRational(v, denominator) for v in lat)
    if lon_ref is not None:
        ifd[GPS_LONGITUDE_REF] = lon_ref
    if lon is not None:
        ifd[GPS_LONGITUDE] = tuple(IFDRational(v, denominator) for v in lon)
    return exif.tobytes()


def _still(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (8, 8), color)


def jpeg_north_east(path: Path) -> None:
    """N/E refs, whole degrees and minutes → +12.5, +45.25 (Gulf of Aden, open water)."""
    _still((10, 120, 200)).save(
        path,
        "JPEG",
        quality=70,
        exif=_gps_exif((12, 30, 0), "N", (45, 15, 0), "E"),
    )


def jpeg_south_west(path: Path) -> None:
    """S/W refs → the SAME magnitudes NEGATED (-33.75, -25.5). The sign is the whole point: a
    reader that ignores the refs answers the northern hemisphere for a southern photo."""
    _still((200, 120, 10)).save(
        path,
        "JPEG",
        quality=70,
        exif=_gps_exif((33, 45, 0), "S", (25, 30, 0), "W"),
    )


def jpeg_no_gps(path: Path) -> None:
    """No GPS IFD at all → read, and no location ('none', not 'unreadable')."""
    _still((120, 200, 10)).save(path, "JPEG", quality=70)


def jpeg_null_island(path: Path) -> None:
    """Exactly (0, 0) — the value a camera writes when its receiver never got a fix. A real
    position off the coast of Ghana, and never a photograph: refused as no location."""
    _still((60, 60, 60)).save(
        path, "JPEG", quality=70, exif=_gps_exif((0, 0, 0), "N", (0, 0, 0), "E")
    )


def jpeg_zero_denominator(path: Path) -> None:
    """Rationals with a zero denominator (NaN) → no location, and no ZeroDivisionError."""
    _still((30, 90, 90)).save(
        path,
        "JPEG",
        quality=70,
        exif=_gps_exif((12, 30, 0), "N", (45, 15, 0), "E", denominator=0),
    )


def jpeg_missing_refs(path: Path) -> None:
    """Magnitudes with NO ref tags → north and east, the EXIF default (+12.5, +45.25)."""
    _still((90, 30, 90)).save(
        path, "JPEG", quality=70, exif=_gps_exif((12, 30, 0), None, (45, 15, 0), None)
    )


def _png_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """The file's chunks as (type, whole chunk INCLUDING length/type/CRC), in file order."""
    chunks: list[tuple[bytes, bytes]] = []
    i = len(PNG_SIGNATURE)
    while i + 8 <= len(raw):
        length = int.from_bytes(raw[i : i + 4], "big")
        kind = raw[i + 4 : i + 8]
        end = i + 12 + length
        chunks.append((kind, raw[i:end]))
        i = end
    return chunks


def png_exif_after_idat(path: Path) -> None:
    """A PNG whose eXIf chunk sits AFTER the image data (+5.0, -30.0).

    Both placements are legal. This one is the placement `im.info["exif"]` never sees, and asking
    Pillow for it anyway (`getexif()`) falls through to a full pixel decode — the one thing the
    reader promises never to do. The fixture is what forces the chunk walk.
    """
    _still((10, 200, 120)).save(path, "PNG", exif=_gps_exif((5, 0, 0), "N", (30, 0, 0), "W"))
    source = _png_chunks(path.read_bytes())
    exif = [chunk for kind, chunk in source if kind == b"eXIf"]
    assert exif, "the PNG save wrote no eXIf chunk to move"
    head = [chunk for kind, chunk in source if kind not in (b"eXIf", b"IEND")]
    tail = [chunk for kind, chunk in source if kind == b"IEND"]
    path.write_bytes(PNG_SIGNATURE + b"".join(head) + b"".join(exif) + b"".join(tail))


def tiff_gps(path: Path) -> None:
    """A TIFF's GPS IFD lives in the file's own first IFD (-7.25, +80.5)."""
    _still((200, 200, 30)).save(path, "TIFF", exif=_gps_exif((7, 15, 0), "S", (80, 30, 0), "E"))


def heic_gps(path: Path) -> None:
    """HEIC carries the same EXIF payload as a JPEG, on its primary item (+20.0, -150.0)."""
    heif = pillow_heif.from_pillow(_still((10, 60, 200)))
    heif.info["exif"] = _gps_exif((20, 0, 0), "N", (150, 0, 0), "W")
    heif.save(path, quality=50)


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", *args], check=True)


def _exiftool(args: list[str]) -> None:
    # stderr is swallowed: exiftool prints a perl locale warning on this workstation that has
    # nothing to do with the file it just wrote.
    subprocess.run(
        ["exiftool", "-overwrite_original", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _clip(path: Path, color: str) -> None:
    _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=32x32:d=0.5:r=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ]
    )


def mov_iso6709(path: Path) -> None:
    """`com.apple.quicktime.location.ISO6709` under moov/meta — what every iPhone MOV carries.

    Written WITH an altitude component (+35 m), because the altitude is the part a lat/lon regex
    most easily swallows into the longitude (+40.1, -20.2).
    """
    _clip(path, "red")
    _exiftool(["-Keys:GPSCoordinates=+40.1000-020.2000+035.000/", str(path)])


def mp4_xyz(path: Path) -> None:
    """`©xyz` under moov/udta — the MP4 spelling, no altitude (+10.5, +150.5).

    Its payload is a QuickTime text atom (2-byte length, 2-byte language, then the string), NOT
    the `data` sub-box the keys/ilst path uses.
    """
    _clip(path, "green")
    _exiftool(["-UserData:GPSCoordinates=+10.5000+150.5000/", str(path)])


def mov_no_location(path: Path) -> None:
    """A clip with no location key anywhere → read, and no location."""
    _clip(path, "blue")


FIXTURES = {
    "jpeg-north-east.jpg": jpeg_north_east,
    "jpeg-south-west.jpg": jpeg_south_west,
    "jpeg-no-gps.jpg": jpeg_no_gps,
    "jpeg-null-island.jpg": jpeg_null_island,
    "jpeg-zero-denominator.jpg": jpeg_zero_denominator,
    "jpeg-missing-refs.jpg": jpeg_missing_refs,
    "png-exif-after-idat.png": png_exif_after_idat,
    "tiff-gps.tif": tiff_gps,
    "heic-gps.heic": heic_gps,
    "mov-iso6709.mov": mov_iso6709,
    "mp4-xyz.mp4": mp4_xyz,
    "mov-no-location.mov": mov_no_location,
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in FIXTURES.items():
        path = OUT / name
        path.unlink(missing_ok=True)
        build(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{name}\t{path.stat().st_size}\t{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------------------------
# HEIF VARIANTS — built at test runtime, never committed (MEMNG-341)
# --------------------------------------------------------------------------------------------
# These are not in FIXTURES: the large one is megabytes of filler (the point of it), and the
# others differ from `heic-gps.heic` only in how the SAME payload is addressed, so pinning four
# more binaries by sha256 would buy nothing the builder below does not already say. Each is
# derived from the committed `heic-gps.heic` rather than authored from scratch, so the image item
# stays a real HEVC primary that libheif can still open — which is what makes the fallback leg of
# `capture.heif_exif_from` testable at all.


def _iso_boxes(buf: bytes, start: int, end: int) -> list[tuple[bytes, int, int, int]]:
    """(type, box start, body start, box end) for the ISO-BMFF boxes in `buf[start:end]`.

    Deliberately its own parser rather than the reader's: a fixture built with the code under test
    proves only that the code agrees with itself.
    """
    found: list[tuple[bytes, int, int, int]] = []
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i : i + 4], "big")
        kind = buf[i + 4 : i + 8]
        header = 8
        if size == 1:
            size = int.from_bytes(buf[i + 8 : i + 16], "big")
            header = 16
        elif size == 0:
            size = end - i
        if size < header or i + size > end:
            break
        found.append((kind, i, i + header, i + size))
        i += size
    return found


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def _large_box(kind: bytes, payload: bytes) -> bytes:
    """The 64-bit `size == 1` spelling a >4 GiB `mdat` needs — and the length branch a header
    walker most easily reads as a 1-byte box."""
    return (1).to_bytes(4, "big") + kind + (16 + len(payload)).to_bytes(8, "big") + payload


def _full_box(kind: bytes, version: int, payload: bytes) -> bytes:
    return _box(kind, bytes([version]) + b"\x00\x00\x00" + payload)


def _iloc(
    items: list[tuple[int, int, int]],
    version: int,
    *,
    construction: dict[int, int] | None = None,
    data_reference: dict[int, int] | None = None,
    index_size: int = 0,
) -> bytes:
    """An `iloc` box in its version 0, 1 or 2 spelling, one extent per item.

    `items` is (item_ID, extent_offset, extent_length). `construction` gives an item a
    construction_method other than 0 (1 = the offset is into `idat`, 2 = into another item), and
    `data_reference` a non-zero data_reference_index (the extent lives in ANOTHER file) — both
    only expressible from version 1 up, and both shapes the header walk must decline to read.
    `index_size` widens the per-extent `extent_index` field that versions 1 and 2 may carry: it
    sits BEFORE the offset and the length, and a reader that forgets it decodes the wrong number.
    """
    construction = construction or {}
    data_reference = data_reference or {}
    offset_size, length_size, base_offset_size = 4, 4, 0
    if version == 0:
        index_size = 0
    body = bytes(
        [
            (offset_size << 4) | length_size,
            (base_offset_size << 4) | (index_size if version in (1, 2) else 0),
        ]
    )
    body += len(items).to_bytes(4 if version == 2 else 2, "big")
    for item_id, offset, length in items:
        body += item_id.to_bytes(4 if version == 2 else 2, "big")
        if version in (1, 2):
            body += construction.get(item_id, 0).to_bytes(2, "big")
        body += data_reference.get(item_id, 0).to_bytes(2, "big")
        body += (1).to_bytes(2, "big")  # extent_count
        if index_size:
            body += (1).to_bytes(index_size, "big")  # extent_index, 1-based
        body += offset.to_bytes(offset_size, "big") + length.to_bytes(length_size, "big")
    return _full_box(b"iloc", version, body)


def _source_items(
    source: bytes, meta_body: int, meta_end: int
) -> tuple[list[int], dict[int, bytes]]:
    """The item IDs in file order and each one's payload, out of a version-0 `iloc`.

    Only the shape `heic-gps.heic` actually carries is accepted (4-byte offsets, lengths and base
    offsets, one extent per item); anything else means the committed fixture changed under this
    builder and is refused rather than mis-read.
    """
    children = {kind: (body, end) for kind, _, body, end in _iso_boxes(source, meta_body, meta_end)}
    body, _end = children[b"iloc"]
    assert source[body] == 0, "the committed HEIC's iloc is version 0"
    assert source[body + 4] == 0x44 and source[body + 5] == 0x40, "4-byte offsets/lengths/bases"
    count = int.from_bytes(source[body + 6 : body + 8], "big")
    i = body + 8
    order: list[int] = []
    payloads: dict[int, bytes] = {}
    for _ in range(count):
        item_id = int.from_bytes(source[i : i + 2], "big")
        base = int.from_bytes(source[i + 4 : i + 8], "big")
        extents = int.from_bytes(source[i + 8 : i + 10], "big")
        assert extents == 1, "one extent per item"
        offset = int.from_bytes(source[i + 10 : i + 14], "big")
        length = int.from_bytes(source[i + 14 : i + 18], "big")
        order.append(item_id)
        payloads[item_id] = source[base + offset : base + offset + length]
        i += 18
    return order, payloads


def _heif_variant(
    source: bytes,
    *,
    iloc_version: int = 0,
    exif_construction: int = 0,
    exif_data_reference: int = 0,
    exif_length: int = 0,
    index_size: int = 0,
    filler: int = 0,
    lead_filler: int = 0,
    trail_filler: int = 0,
    large_mdat: bool = False,
) -> bytes:
    """`heic-gps.heic` re-laid-out: the same items, addressed a different legal way.

    What changes is only HOW the bytes are addressed — the `iloc` version, the Exif item's
    construction_method, data_reference_index and declared extent LENGTH, how much filler
    surrounds the payloads, and whether `mdat` carries the 64-bit length. The image item and every
    property box are carried over untouched, so the result is still a HEIC libheif opens: that is
    what lets one fixture prove both the header walk and the full read it falls back to.
    """
    top = {
        kind: (start, body, end) for kind, start, body, end in _iso_boxes(source, 0, len(source))
    }
    ftyp_start, _ftyp_body, ftyp_end = top[b"ftyp"]
    _meta_start, meta_body, meta_end = top[b"meta"]
    ftyp = source[ftyp_start:ftyp_end]
    children = _iso_boxes(source, meta_body + 4, meta_end)  # meta is a FullBox
    order, payloads = _source_items(source, meta_body + 4, meta_end)
    exif_id = _exif_item_id(source, meta_body + 4, meta_end)
    # Only construction_method 1 MOVES the payload; method 2 is written as a lie about where the
    # extent lives, which is all a reader that must decline to follow it needs to see.
    in_idat = exif_construction == 1
    in_mdat = [item for item in order if not (in_idat and item == exif_id)]
    construction = {exif_id: exif_construction} if exif_construction else None
    data_reference = {exif_id: exif_data_reference} if exif_data_reference else None
    # A 64-bit `free` box BEFORE `meta`: the box a header walker has to skip by its extended
    # length, and megabytes it must never pull into memory.
    lead = _large_box(b"free", b"\x00" * lead_filler) if lead_filler else b""

    rows = [(item, 0, len(payloads[item])) for item in order]
    out = b""
    for _pass in (1, 2):  # the field widths are fixed, so the second pass only fills the offsets
        pieces: list[bytes] = []
        for kind, start, _body, end in children:
            if kind != b"iloc":
                pieces.append(source[start:end])
                continue
            if in_idat:
                pieces.append(_box(b"idat", payloads[exif_id]))
            pieces.append(
                _iloc(
                    rows,
                    iloc_version,
                    construction=construction,
                    data_reference=data_reference,
                    index_size=index_size,
                )
            )
        meta = _box(b"meta", b"\x00\x00\x00\x00" + b"".join(pieces))
        mdat_payload = (
            b"\x00" * filler + b"".join(payloads[i] for i in in_mdat) + b"\x00" * trail_filler
        )
        mdat = (_large_box if large_mdat else _box)(b"mdat", mdat_payload)
        cursor = len(ftyp) + len(lead) + len(meta) + (16 if large_mdat else 8) + filler
        rows = []
        for item in order:
            # The DECLARED length may be a lie (`exif_length`); the layout still uses the real one.
            declared = exif_length if exif_length and item == exif_id else len(payloads[item])
            if in_idat and item == exif_id:
                rows.append((item, 0, declared))  # an offset INTO the idat box
            else:
                rows.append((item, cursor, declared))
                cursor += len(payloads[item])
        out = ftyp + lead + meta + mdat
    return out


def _exif_item_id(source: bytes, meta_body: int, meta_end: int) -> int:
    """The `iinf` entry whose item_type is `Exif`, as its item ID."""
    children = {kind: (body, end) for kind, _, body, end in _iso_boxes(source, meta_body, meta_end)}
    body, end = children[b"iinf"]
    for kind, _start, infe_body, _infe_end in _iso_boxes(source, body + 6, end):
        if kind != b"infe" or source[infe_body] < 2:
            continue
        if source[infe_body + 8 : infe_body + 12] == b"Exif":
            return int.from_bytes(source[infe_body + 4 : infe_body + 6], "big")
    raise AssertionError("the committed HEIC carries no Exif item")


def heic_gps_large(path: Path, *, filler: int = 3 * 1024 * 1024) -> None:
    """`heic-gps.heic` grown to megabytes: a 64-bit `free` box before `meta`, and megabytes of
    `mdat` before the payloads, under a 64-bit `mdat` length.

    The file the header walker exists for: a reader that opens it with libheif pays for every one
    of those bytes over NFS, a reader that walks the boxes pays for the `meta` box and the Exif
    extent. Not committed — its size IS the fixture.
    """
    path.write_bytes(
        _heif_variant(_source_heic(), filler=filler, lead_filler=1024 * 1024, large_mdat=True)
    )


def heic_gps_iloc(path: Path, version: int) -> None:
    """`heic-gps.heic` with its `iloc` rewritten in version 0, 1 or 2 — the three spellings a real
    library carries (1 and 2 add the construction_method, 2 widens the item ID and the count)."""
    path.write_bytes(_heif_variant(_source_heic(), iloc_version=version))


def heic_gps_idat(path: Path) -> None:
    """`heic-gps.heic` with the Exif item stored in `idat` (construction_method 1).

    Legal, and deliberately NOT followed by the header walk: an idat extent is addressed inside
    the `meta` box rather than in the file, so the walker answers None and the caller falls back
    to the full read, which still finds the location. The fixture is what makes that a tested path
    instead of a claim.
    """
    path.write_bytes(_heif_variant(_source_heic(), iloc_version=1, exif_construction=1))


def heic_gps_item_offset(path: Path) -> None:
    """`heic-gps.heic` whose Exif extent claims construction_method 2 (an offset into another
    item) — the third method, and the second one the walker must decline rather than guess at."""
    path.write_bytes(_heif_variant(_source_heic(), iloc_version=2, exif_construction=2))


def heic_gps_external(path: Path) -> None:
    """`heic-gps.heic` whose Exif extent names a data_reference_index of 1: the payload is in
    ANOTHER file entirely, so an offset read against this one would return unrelated bytes."""
    path.write_bytes(_heif_variant(_source_heic(), iloc_version=1, exif_data_reference=1))


def heic_gps_overlong_exif(path: Path, *, length: int = 3 * 1024 * 1024) -> None:
    """`heic-gps.heic` whose `iloc` claims a MEGABYTES-long Exif extent.

    What a corrupt or hostile item table looks like: the offset is real, the length is not, and a
    reader that believes it allocates whatever the header asks for. The filler after the payload
    is there so the overlong read would SUCCEED if the cap were gone — a fixture that simply ran
    past the end of the file would be declined by the short read instead, and would prove nothing
    about the cap.
    """
    path.write_bytes(_heif_variant(_source_heic(), exif_length=length, trail_filler=length))


def heic_gps_index_size(path: Path, version: int, index_size: int) -> None:
    """`heic-gps.heic` whose extents carry an `extent_index` of 4 or 8 bytes.

    Versions 1 and 2 of `iloc` may put an index before every extent's offset and length. Its width
    is declared once, in a nibble, and a reader that skips it — or that measures the length from
    the wrong end of it — decodes a length of billions from a file of hundreds of bytes.
    """
    path.write_bytes(_heif_variant(_source_heic(), iloc_version=version, index_size=index_size))


# The in-place damages: a byte or two rewritten in a file that is otherwise laid out correctly, so
# every offset in it stays valid and libheif can still open it. That is what makes them a test of
# what the READER does with a structure it cannot trust, rather than of whether a file is readable.
DAMAGES = {
    "meta-version": ("meta", 0, b"\x01"),  # a `meta` FullBox version nobody has defined
    "iinf-version": ("iinf", 0, b"\x02"),
    "pitm-version": ("pitm", 0, b"\x02"),
    "iref-version": ("iref", 0, b"\x02"),
    "iloc-version": ("iloc", 0, b"\x03"),
    "iinf-count": ("iinf", 4, b"\x00\x09"),  # nine entries declared, two present
}


def heic_gps_damaged(path: Path, damage: str) -> None:
    """`heic-gps.heic` with one structural lie in its `meta` box — see `DAMAGES`."""
    buf = bytearray(_heif_variant(_source_heic(), iloc_version=1))
    kind, at, replacement = DAMAGES[damage]
    if damage == "meta-version":
        _, _, meta_body, _ = next(b for b in _iso_boxes(buf, 0, len(buf)) if b[0] == b"meta")
        buf[meta_body + at : meta_body + at + len(replacement)] = replacement
    else:
        box = _meta_child(buf, kind.encode())
        buf[box + at : box + at + len(replacement)] = replacement
    path.write_bytes(bytes(buf))


def heic_gps_infe_overrun(path: Path) -> None:
    """`heic-gps.heic` whose LAST `infe` declares a size running past the end of `iinf`.

    The entry list stops short and the table that comes out of it is partial — a reader that hands
    on what it collected answers from half a file it already knows is malformed.
    """
    buf = bytearray(_heif_variant(_source_heic(), iloc_version=1))
    iinf = _meta_child(buf, b"iinf")
    end = _meta_child_end(buf, b"iinf")
    entries = _iso_boxes(buf, iinf + 6, end)
    last = entries[-1]
    size = last[3] - last[1] + 64
    buf[last[1] : last[1] + 4] = size.to_bytes(4, "big")
    path.write_bytes(bytes(buf))


def heic_exif_describes_secondary(path: Path) -> None:
    """`heic-offset.heic` reduced to ONE Exif item — the one describing the SECONDARY image.

    That file carries two images and two Exif items, `pitm` naming the second image and `cdsc`
    linking one Exif to each. Retyping the primary's Exif entry leaves a file with exactly one
    Exif item that belongs to the image nobody asked for; every byte offset stays where it was, so
    libheif still opens it. A reader that treats "only one Exif item" as "the primary's Exif"
    answers this file with the wrong image's metadata, and returning bytes leaves the caller no
    fallback to be saved by.
    """
    source = Path(__file__).resolve().parent.parent / "fixtures" / "auto-memo-date"
    buf = bytearray((source / "heic-offset.heic").read_bytes())
    primary = _heif_primary(buf)
    described = _heif_described_by(buf)
    for item, describes in described.items():
        if describes == primary:
            _retype_item(buf, item, b"mime")  # a four-character type, so nothing moves
    path.write_bytes(bytes(buf))


def _heif_primary(buf: bytes) -> int:
    pitm = _meta_child(buf, b"pitm")
    return int.from_bytes(buf[pitm + 4 : pitm + 6], "big")


def _heif_described_by(buf: bytes) -> dict[int, int]:
    """Each `cdsc` reference as {describing item: described item}."""
    iref, end = _meta_child(buf, b"iref"), _meta_child_end(buf, b"iref")
    links = {}
    for kind, _start, body, _box_end in _iso_boxes(buf, iref + 4, end):
        if kind == b"cdsc":
            links[int.from_bytes(buf[body : body + 2], "big")] = int.from_bytes(
                buf[body + 4 : body + 6], "big"
            )
    return links


def _retype_item(buf: bytearray, item: int, kind: bytes) -> None:
    iinf, end = _meta_child(buf, b"iinf"), _meta_child_end(buf, b"iinf")
    for found, _start, body, _box_end in _iso_boxes(buf, iinf + 6, end):
        if found == b"infe" and int.from_bytes(buf[body + 4 : body + 6], "big") == item:
            buf[body + 8 : body + 12] = kind


def _meta_child(buf: bytes, kind: bytes) -> int:
    """Where one `meta` child box's BODY starts (its FullBox version byte, for the damages)."""
    _, _, meta_body, meta_end = next(b for b in _iso_boxes(buf, 0, len(buf)) if b[0] == b"meta")
    return next(b for b in _iso_boxes(buf, meta_body + 4, meta_end) if b[0] == kind)[2]


def _meta_child_end(buf: bytes, kind: bytes) -> int:
    _, _, meta_body, meta_end = next(b for b in _iso_boxes(buf, 0, len(buf)) if b[0] == b"meta")
    return next(b for b in _iso_boxes(buf, meta_body + 4, meta_end) if b[0] == kind)[3]


def _source_heic() -> bytes:
    return (OUT / "heic-gps.heic").read_bytes()
