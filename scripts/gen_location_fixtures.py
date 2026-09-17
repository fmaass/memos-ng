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
