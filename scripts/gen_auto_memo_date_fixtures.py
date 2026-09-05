"""Author the seven binary fixtures behind `fixtures/auto-memo-date.json`.

Run it only to (re)create them; the committed files are the contract, and their sha256 is
recorded in the artefact so a silent edit fails the suite. ffmpeg and exiftool are used HERE,
at authoring time, on a workstation — the app never depends on either for reading a capture
date (`app/services/capture.py` is Pillow + a pure-Python moov reader).

    .venv/bin/python scripts/gen_auto_memo_date_fixtures.py

Each file is deliberately tiny (8x8 stills, 0.5 s 32x32 clips) and carries exactly one
interesting fact, so a failure names a rule rather than a file.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pillow_heif
from PIL import ExifTags, Image

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "auto-memo-date"

DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011


def _exif(dt: str, offset: str | None) -> Image.Exif:
    exif = Image.Exif()
    ifd = exif.get_ifd(ExifTags.IFD.Exif)
    ifd[DATETIME_ORIGINAL] = dt
    if offset is not None:
        ifd[OFFSET_TIME_ORIGINAL] = offset
    return exif


def _still(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (8, 8), color)


def jpeg_offset(path: Path) -> None:
    """DateTimeOriginal + OffsetTimeOriginal → an exact instant (13:00 +05:45 = 07:15Z)."""
    _still((10, 120, 200)).save(
        path, "JPEG", quality=70, exif=_exif("2026:01:01 13:00:00", "+05:45").tobytes()
    )


def jpeg_no_offset(path: Path) -> None:
    """DateTimeOriginal alone → a CIVIL time; only the client can resolve it to an instant."""
    _still((200, 120, 10)).save(
        path, "JPEG", quality=70, exif=_exif("2026:01:01 13:00:00", None).tobytes()
    )


def heic_offset(path: Path) -> None:
    """Two images, the PRIMARY one SECOND — the reader must not take the first item's date.

    The decoy (item 0) carries a different DateTimeOriginal, so a reader that ignores the
    primary-image marker produces 2019 instead of the expected 2026 instant.
    """
    heif = pillow_heif.from_pillow(_still((30, 30, 30)))  # decoy, item 0
    heif.info["exif"] = _exif("2019:07:07 07:07:07", "+00:00").tobytes()
    primary = heif.add_from_pillow(_still((10, 200, 120)))  # item 1 — the primary
    primary.info["exif"] = _exif("2026:01:01 13:00:00", "+05:45").tobytes()
    heif.save(path, quality=50, primary_index=1)


def png_none(path: Path) -> None:
    """No date metadata at all → no instant, no civil time, no source."""
    _still((120, 200, 10)).save(path, "PNG")


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", *args], check=True)


def _clip(
    path: Path, color: str, *, pre: list[str] | None = None, post: list[str] | None = None
) -> None:
    _ffmpeg(
        [
            *(pre or []),
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=32x32:d=0.5:r=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            *(post or []),
            "-y",
            str(path),
        ]
    )


def mov_creationdate(path: Path) -> None:
    """`com.apple.quicktime.creationdate` (13:00+0200 = 11:00Z) WINS over the movie header.

    The header is deliberately set to 2019 so a reader that skips the keys/ilst atoms and falls
    straight through to mvhd fails loudly instead of accidentally agreeing.
    """
    _clip(path, "red", post=["-metadata", "creation_time=2019-05-05T00:00:00Z"])
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-QuickTime:CreationDate=2026:01:01 13:00:00+02:00",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def mp4_header_only(path: Path) -> None:
    """No creationdate key — the movie header's UTC seconds are the only date."""
    _clip(path, "green", post=["-metadata", "creation_time=2026-02-03T09:30:00Z"])


def mp4_zero_header(path: Path) -> None:
    """A zero movie header (the 1904 epoch, i.e. "unset") → rejected, never 1904-01-01."""
    _clip(path, "blue", pre=["-fflags", "+bitexact"], post=["-flags:v", "+bitexact"])


FIXTURES = {
    "jpeg-offset.jpg": jpeg_offset,
    "jpeg-no-offset.jpg": jpeg_no_offset,
    "heic-offset.heic": heic_offset,
    "png-none.png": png_none,
    "mov-creationdate.mov": mov_creationdate,
    "mp4-header-only.mp4": mp4_header_only,
    "mp4-zero-header.mp4": mp4_zero_header,
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
