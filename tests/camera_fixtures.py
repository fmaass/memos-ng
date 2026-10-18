"""Camera-EXIF fixture images, authored at TEST RUNTIME with Pillow and pillow-heif (MEMNG-865).

Nothing here is committed as a binary: each builder writes one tiny 8x8 still into a directory the
test owns, carrying exactly the tags that file is about, so a failure names a rule rather than a
file. The camera names are invented.
"""

import struct
import zlib
from pathlib import Path

import pillow_heif
from app.services import capture
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from scripts import gen_location_fixtures as gen

MAKE = 0x010F
MODEL = 0x0110
EXIF_IFD = 0x8769
LENS_MODEL = 0xA434
F_NUMBER = 0x829D
EXPOSURE_TIME = 0x829A
ISO = 0x8827
FOCAL_LENGTH = 0x920A
EXPOSURE_BIAS = 0x9204

# What `full_jpeg` carries, as the reader must hand it back.
FULL = {
    "make": "ACME",
    "model": "Fieldcam 9",
    "lens_model": "ACME 35mm F1.8",
    "f_number": 2.8,
    "exposure_time_s": 0.004,
    "focal_length_mm": 35.0,
    "exposure_bias_ev": -0.3,
    "iso": 400,
}


def exif_bytes(ifd0: dict[int, object], sub: dict[int, object]) -> bytes:
    exif = Image.Exif()
    for tag, value in ifd0.items():
        exif[tag] = value
    if sub:
        ifd = exif.get_ifd(EXIF_IFD)
        for tag, value in sub.items():
            ifd[tag] = value
    return exif.tobytes()


def full_tags() -> tuple[dict[int, object], dict[int, object]]:
    return (
        {MAKE: "  ACME  ", MODEL: "Fieldcam 9"},
        {
            LENS_MODEL: "ACME 35mm F1.8",
            F_NUMBER: IFDRational(28, 10),
            EXPOSURE_TIME: IFDRational(1, 250),
            ISO: 400,
            FOCAL_LENGTH: IFDRational(35, 1),
            EXPOSURE_BIAS: IFDRational(-3, 10),
        },
    )


def jpeg(path: Path, ifd0: dict[int, object], sub: dict[int, object]) -> Path:
    Image.new("RGB", (8, 8), (40, 90, 160)).save(path, "JPEG", exif=exif_bytes(ifd0, sub))
    return path


def full_jpeg(path: Path) -> Path:
    """All eight fields, the bias negative and the make padded, so stripping and sign both show."""
    return jpeg(path, *full_tags())


def heic(path: Path, ifd0: dict[int, object], sub: dict[int, object] | None = None) -> Path:
    heif = pillow_heif.from_pillow(Image.new("RGB", (8, 8), (10, 60, 200)))
    heif.info["exif"] = exif_bytes(ifd0, sub or {})
    heif.save(path, quality=50)
    return path


def make_model_heic(path: Path) -> Path:
    return heic(path, {MAKE: "Pomme", MODEL: "Phone 16"})


def bare_jpeg(path: Path) -> Path:
    """A JPEG with no EXIF segment at all."""
    Image.new("RGB", (8, 8), (200, 30, 30)).save(path, "JPEG")
    return path


def dated_only_jpeg(path: Path) -> Path:
    """EXIF present, but not one camera tag in it — a date only."""
    return jpeg(path, {}, {0x9003: "2021:06:01 12:00:00"})


def corrupt(path: Path) -> Path:
    """Bytes no image decoder recognises, under an image name."""
    path.write_bytes(b"\xff\xd8\xff\xe1 this is not an image at all " * 8)
    return path


def invalid_only_jpeg(path: Path) -> Path:
    """The ONLY camera field is out of range (ISO beyond PostgreSQL INTEGER) → read, and 'none'."""
    return jpeg(path, {}, {ISO: 4294967295})


def huge_iso_jpeg(path: Path) -> Path:
    tags0, sub = full_tags()
    return jpeg(path, tags0, {**sub, ISO: 4294967295})


def zero_denominator_jpeg(path: Path) -> Path:
    tags0, sub = full_tags()
    return jpeg(path, tags0, {**sub, F_NUMBER: IFDRational(28, 0)})


def nul_make_jpeg(path: Path) -> Path:
    tags0, sub = full_tags()
    return jpeg(path, {**tags0, MAKE: "ACME\x00BODY"}, sub)


def tiny_mp4(path: Path) -> Path:
    """Not an image; its bytes are never meant to be read by the camera path."""
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return path


# The diff gate's reproduction: a TIFF header whose IFD0 declares one entry and holds none. Pillow
# does not raise on it — it warns "Corrupt EXIF data" and hands back an empty directory.
TRUNCATED_IFD = bytes.fromhex("45786966000049492a00080000000100")


def truncated_ifd_jpeg(path: Path) -> Path:
    Image.new("RGB", (8, 8), (90, 90, 90)).save(path, "JPEG", exif=TRUNCATED_IFD)
    return path


def truncated_ifd_heic(path: Path) -> Path:
    heif = pillow_heif.from_pillow(Image.new("RGB", (8, 8), (10, 60, 200)))
    heif.info["exif"] = TRUNCATED_IFD
    heif.save(path, quality=50)
    return path


def _ifd_with_make_then(sub_offset: int) -> bytes:
    """A little-endian payload: IFD0 = Make "ACME" inline + an Exif-IFD pointer to `sub_offset`."""
    entries = (
        (MAKE).to_bytes(2, "little")
        + (2).to_bytes(2, "little")
        + (4).to_bytes(4, "little")
        + b"ACM\x00"
        + (EXIF_IFD).to_bytes(2, "little")
        + (4).to_bytes(2, "little")
        + (1).to_bytes(4, "little")
        + sub_offset.to_bytes(4, "little")
    )
    return (
        b"Exif\x00\x00II*\x00"
        + (8).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + entries
        + (0).to_bytes(4, "little")
    )


def dangling_sub_ifd_jpeg(path: Path) -> Path:
    """IFD0 is whole and names a Make; the Exif sub-IFD it points at lies past the payload's end."""
    Image.new("RGB", (8, 8), (90, 90, 90)).save(path, "JPEG", exif=_ifd_with_make_then(4000))
    return path


def dangling_value_jpeg(path: Path) -> Path:
    """IFD0's only entry is a 20-byte Make whose value offset lies past the payload's end."""
    entry = (
        (MAKE).to_bytes(2, "little")
        + (2).to_bytes(2, "little")
        + (20).to_bytes(4, "little")
        + (4000).to_bytes(4, "little")
    )
    raw = (
        b"Exif\x00\x00II*\x00"
        + (8).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + entry
        + (0).to_bytes(4, "little")
    )
    Image.new("RGB", (8, 8), (90, 90, 90)).save(path, "JPEG", exif=raw)
    return path


def tiff_with_camera(path: Path) -> Path:
    tags0, sub = full_tags()
    Image.new("RGB", (8, 8), (200, 200, 30)).save(path, "TIFF", exif=exif_bytes(tags0, sub))
    return path


def tiff_file(path: Path, *, mode: str, big: bool, tagged: bool) -> Path:
    """A Pillow-written TIFF: mode "RGB" writes little-endian (II), "I;16B" big-endian (MM)."""
    info = {MAKE: "ACME", MODEL: "M"} if tagged else {}
    Image.new(mode, (8, 8)).save(path, "TIFF", big_tiff=big, tiffinfo=info)
    return path


def big_endian_exif_jpeg(path: Path) -> Path:
    """The camera EXIF as an MM (Motorola) payload inside a JPEG."""
    tags0, sub = full_tags()
    exif = Image.Exif()
    exif.endian = ">"
    for tag, value in tags0.items():
        exif[tag] = value
    ifd = exif.get_ifd(EXIF_IFD)
    for tag, value in sub.items():
        ifd[tag] = value
    Image.new("RGB", (8, 8), (40, 90, 160)).save(path, "JPEG", exif=exif.tobytes())
    return path


def bad_bigtiff_header(path: Path, *, bytesize: int = 8, reserved: int = 0) -> Path:
    """A BigTIFF whose header's offset size or reserved word is not what the format fixes."""
    tiff_file(path, mode="RGB", big=True, tagged=True)
    raw = bytearray(path.read_bytes())
    raw[4:6] = bytesize.to_bytes(2, "little")
    raw[6:8] = reserved.to_bytes(2, "little")
    path.write_bytes(bytes(raw))
    return path


# --- an EXIF payload a container DECLARES but cannot complete -----------------------------------
#
# Each builder below wraps `payload` VERBATIM in one container's own EXIF carrier, so one list of
# payloads exercises every path a declared payload reaches the reader by.

EXIF_PREFIX = b"Exif\x00\x00"
# A complete header and a complete, EMPTY IFD0 — the smallest payload that is read and silent.
EMPTY_CLASSIC = b"II*\x00\x08\x00\x00\x00" + b"\x00\x00" + b"\x00\x00\x00\x00"
EMPTY_BIG = b"II+\x00\x08\x00\x00\x00" + struct.pack("<Q", 16) + bytes(8) + bytes(8)
# A complete classic header whose IFD0 lies 4 KB past the end of the payload.
PAST_END = b"II*\x00\x00\x10\x00\x00"


def jpeg_app1(path: Path, payload: bytes) -> Path:
    """A JPEG with one APP1 segment holding `payload` — EXIF only if it starts `Exif\\0\\0`."""
    Image.new("RGB", (8, 8), (40, 90, 160)).save(path, "JPEG")
    raw = path.read_bytes()
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    path.write_bytes(raw[:2] + segment + raw[2:])
    return path


def png_exif_chunk(path: Path, payload: bytes, *, after_idat: bool) -> Path:
    """A PNG with one `eXIf` chunk holding `payload`, before or after the image data."""
    Image.new("RGB", (8, 8), (40, 90, 160)).save(path, "PNG")
    raw = path.read_bytes()
    chunk = (
        struct.pack(">I", len(payload))
        + b"eXIf"
        + payload
        + struct.pack(">I", zlib.crc32(b"eXIf" + payload))
    )
    at = raw.index(b"IEND") - 4 if after_idat else 8 + 25  # after IEND's length / after IHDR
    path.write_bytes(raw[:at] + chunk + raw[at:])
    return path


def webp_exif_chunk(path: Path, payload: bytes) -> Path:
    """An extended (VP8X) WebP whose `EXIF` chunk holds `payload` — zero bytes included."""
    Image.new("RGB", (8, 8), (40, 90, 160)).save(path, "WEBP", exif=EXIF_PREFIX + EMPTY_CLASSIC)
    raw = path.read_bytes()
    chunks, at = [], 12
    while at < len(raw):
        kind, (size,) = raw[at : at + 4], struct.unpack("<I", raw[at + 4 : at + 8])
        body = payload if kind == b"EXIF" else raw[at + 8 : at + 8 + size]
        chunks.append(kind + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) % 2))
        at += 8 + size + size % 2
    riff = b"WEBP" + b"".join(chunks)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff)) + riff)
    return path


# libheif refuses to WRITE an Exif item it cannot find a TIFF header in, so a short payload is
# written as a complete one and its item's extent then DECLARED short: what a reader sees is
# exactly `payload`, after the item's 4-byte header-offset field.
_HEIF_TEMPLATES = (
    EXIF_PREFIX + EXIF_PREFIX + EMPTY_CLASSIC,
    EXIF_PREFIX + EMPTY_CLASSIC,
    EXIF_PREFIX + EMPTY_BIG,
    EXIF_PREFIX + PAST_END,
    EMPTY_CLASSIC,
    EMPTY_BIG,
    PAST_END,
)


def heif_exif_extent(path: Path, payload: bytes) -> Path:
    """A HEIC whose Exif item carries exactly `payload` — an empty one is the 4-byte block alone."""
    template = next(t for t in _HEIF_TEMPLATES if t.startswith(payload))
    source = path.with_name(path.name + ".source.heic")
    heif = pillow_heif.from_pillow(Image.new("RGB", (8, 8), (10, 60, 200)))
    heif.info["exif"] = template
    heif.save(source, quality=50)
    with open(source, "rb") as f:
        meta = capture._heif_meta_from(f)
    assert meta is not None
    children = capture._heif_children(meta)
    assert children is not None
    item = capture._heif_exif_item(meta, children)
    assert item is not None
    extent = capture._heif_extent(meta, children, item)
    assert extent is not None
    declared = extent[1] - len(template) + len(payload)
    path.write_bytes(gen._heif_variant(source.read_bytes(), exif_length=declared))
    return path


def oversized_exif() -> bytes:
    """A bare TIFF payload with Make=ACME and a MakerNote that takes it past the PNG read cap."""
    raw = exif_bytes({MAKE: "ACME"}, {0x927C: b"\x00" * (capture.PNG_EXIF_READ_CAP + 48)})
    return raw.removeprefix(EXIF_PREFIX)


def _webp_chunks(path: Path) -> list[tuple[bytes, bytes]]:
    """The chunks of a Pillow-written extended (VP8X) WebP whose EXIF flag is set."""
    webp_exif_chunk(path, EXIF_PREFIX + EMPTY_CLASSIC)
    raw, chunks, at = path.read_bytes(), [], 12
    while at < len(raw):
        kind, (size,) = raw[at : at + 4], struct.unpack("<I", raw[at + 4 : at + 8])
        chunks.append((kind, raw[at + 8 : at + 8 + size]))
        at += 8 + size + size % 2
    return chunks


def webp_container(path: Path, chunks: list[tuple[bytes, bytes]], *, trailing: bytes = b"") -> Path:
    """`chunks` as a RIFF/WEBP file whose size covers exactly them; `trailing` lies OUTSIDE it."""
    body = b"".join(
        kind + struct.pack("<I", len(data)) + data + b"\x00" * (len(data) % 2)
        for kind, data in chunks
    )
    riff = b"WEBP" + body
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff)) + riff + trailing)
    return path


def webp_vp8x_without_exif_chunk(path: Path, *, flag: bool) -> Path:
    """A VP8X WebP with its EXIF chunk removed — the header's EXIF flag kept or cleared."""
    chunks = [(k, d) for k, d in _webp_chunks(path) if k != b"EXIF"]
    kind, data = chunks[0]
    assert kind == b"VP8X" and data[0] & 0x08
    flags = data[0] if flag else data[0] & ~0x08
    chunks[0] = (kind, bytes([flags]) + data[1:])
    return webp_container(path, chunks)


def webp_odd_chunk_then_empty_exif(path: Path) -> Path:
    """An unknown 3-byte chunk (so one pad byte) BEFORE an empty EXIF chunk.

    The VP8X EXIF flag is CLEARED, so the chunk itself is the only declaration a walk can find.
    """
    chunks = _webp_chunks(path)
    kind, data = chunks[0]
    chunks[0] = (kind, bytes([data[0] & ~0x08]) + data[1:])
    at = next(i for i, (k, _d) in enumerate(chunks) if k == b"EXIF")
    chunks[at] = (b"EXIF", b"")
    chunks.insert(at, (b"ABCD", b"xyz"))
    return webp_container(path, chunks)


def webp_with_exif_bytes_appended(path: Path) -> Path:
    """An ordinary untagged WebP with `EXIF` + a zero size appended PAST its RIFF size."""
    Image.new("RGB", (8, 8), (40, 90, 160)).save(path, "WEBP")
    path.write_bytes(path.read_bytes() + b"EXIF" + bytes(4))
    return path
