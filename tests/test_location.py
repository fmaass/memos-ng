"""The location read (Phase 1 of Places): what a file's own metadata says about WHERE it was.

`fixtures/locations.json` is the contract — twelve binaries pinned by sha256, each carrying one
rule — and these cases hold `app/services/location.py` to it. The parametrized fixture test is the
acceptance probe for the reader; the unit cases below it name the individual rules so a failure
says WHICH rule broke rather than which file.

Every coordinate here comes from the artefact, and every coordinate in the artefact is fictional
(open ocean) — see `scripts/gen_location_fixtures.py`.
"""

import hashlib
import json
from pathlib import Path

import pytest
from app.services import location

ARTEFACT = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures/locations.json").read_text()
)
BINARIES = Path(__file__).resolve().parent.parent / "fixtures" / "locations"


def _read(name: str) -> dict[str, object]:
    """What the reader answers for one fixture, in the artefact's own shape."""
    spec = ARTEFACT["binaries"][name]
    result = location.read_location(BINARIES / name, spec["mime"])
    if result is None:
        return {"latitude": None, "longitude": None, "location_source": location.SOURCE_NONE}
    return {
        "latitude": result.latitude,
        "longitude": result.longitude,
        "location_source": result.source,
    }


@pytest.mark.parametrize("name", sorted(ARTEFACT["binaries"]))
def test_binary_fixture_reads_exactly_what_the_artefact_promises(name):
    assert _read(name) == ARTEFACT["binaries"][name]["expect"]


@pytest.mark.parametrize("name", sorted(ARTEFACT["binaries"]))
def test_binary_fixture_bytes_are_the_ones_the_artefact_names(name):
    # The sha256 is what ties the contract to these bytes, so an edited fixture fails here rather
    # than quietly re-specifying what the reader is allowed to answer.
    spec = ARTEFACT["binaries"][name]
    raw = (BINARIES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == spec["sha256"]
    assert len(raw) == spec["size"]


def test_the_artefact_says_its_coordinates_are_fictional():
    # A real household coordinate in a public repository is the one leak this feature cannot take
    # back; the artefact has to say so in its own text, next to the values.
    assert "INVENTED" in ARTEFACT["fictional"]


# --------------------------------------------------------------------------------------------
# The sign rule and the DMS shapes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "ref", "expected"),
    [
        ((12, 30, 0), "N", 12.5),
        ((12, 30, 0), "S", -12.5),
        ((45, 15, 0), "E", 45.25),
        ((45, 15, 0), "W", -45.25),
        ((12, 30, 0), None, 12.5),  # no ref → the EXIF default, north/east
        ((12, 30, 0), "", 12.5),
        ((12, 30, 0), "n", 12.5),  # refs are single letters, case is the camera's business
        ((12, 30, 0), "s", -12.5),
    ],
)
def test_the_ref_decides_the_sign(values, ref, expected):
    assert location.coordinate_from_dms(values, ref) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((12,), 12.0),  # degrees alone
        ((12, 30), 12.5),  # degrees + minutes
        ((12, 30, 36), 12.51),  # degrees + minutes + seconds
        ((12, 0, 0, 0), None),  # four components is not a coordinate
        ((), None),
    ],
)
def test_one_two_or_three_components_are_read_and_nothing_else(values, expected):
    assert location.coordinate_from_dms(values, "N") == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_component_is_no_coordinate(value):
    assert location.coordinate_from_dms((value, 0, 0), "N") is None


def test_a_zero_denominator_is_no_coordinate():
    from PIL.TiffImagePlugin import IFDRational

    assert location.coordinate_from_dms((IFDRational(12, 0),), "N") is None


@pytest.mark.parametrize(("values", "ref"), [((91, 0, 0), "N"), ((90, 0, 1), "S")])
def test_a_latitude_out_of_range_is_no_location(values, ref):
    assert location.location_from_pair(location.coordinate_from_dms(values, ref), 10.0) is None


def test_a_longitude_out_of_range_is_no_location():
    assert location.location_from_pair(10.0, 180.5) is None


def test_null_island_is_no_location():
    # (0, 0) is what a camera writes when the receiver never got a fix. It is a real point in the
    # Atlantic and never a photograph anyone took.
    assert location.location_from_pair(0.0, 0.0) is None
    assert location.location_from_pair(0.0, 0.0001) is not None


def test_a_missing_half_of_the_pair_is_no_location():
    assert location.location_from_pair(12.5, None) is None
    assert location.location_from_pair(None, 45.25) is None


# --------------------------------------------------------------------------------------------
# ISO 6709 (the video spelling)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+40.1000-020.2000+035.000/", (40.1, -20.2)),  # with altitude
        ("+10.5000+150.5000/", (10.5, 150.5)),  # without
        ("-33.7500-025.5000/", (-33.75, -25.5)),
        ("+40.1000-020.2000+035.000CRSWGS_84/", (40.1, -20.2)),  # a CRS suffix is ignored
        ("+12+045/", (12.0, 45.0)),  # degrees only, the shortest legal form
        ("+40.1000-020.2000", (40.1, -20.2)),  # a missing solidus is tolerated, not required
        ("", None),
        ("40.1,-20.2", None),  # a decimal pair without signs is not ISO 6709
        ("+40.1000/", None),  # latitude alone
        ("+91.0000-020.2000/", None),  # out of range
        ("+00.0000+000.0000/", None),  # null island again, through the other reader
        ("nonsense", None),
        (None, None),
    ],
)
def test_iso6709_decimal_forms(text, expected):
    got = location.parse_iso6709(text)
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert (got.latitude, got.longitude) == expected
        assert got.source == location.SOURCE_QUICKTIME


# --------------------------------------------------------------------------------------------
# Failure is absence, never an exception — and never a pixel decode
# --------------------------------------------------------------------------------------------


def test_a_missing_file_raises_so_the_caller_can_call_it_unreadable(tmp_path):
    # `read_location` answers None for "read it, no location" — a file it could not read at all is
    # a different fact (retryable), so it must NOT be flattened into the same answer here.
    with pytest.raises(OSError):
        location.read_location(tmp_path / "gone.jpg", "image/jpeg")


def test_a_truncated_original_is_not_a_location(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe1 not really a jpeg")
    with pytest.raises(Exception):  # noqa: B017 — the CALLER decides what an unreadable file means
        location.read_location(broken, "image/jpeg")


def test_an_unknown_mime_is_read_as_no_location(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_bytes(b"hello")
    assert location.read_location(other, "text/plain") is None


def test_the_png_path_never_decodes_pixels(monkeypatch):
    # The guard MEMNG-262 put on the capture read applies verbatim here: `getexif()` on a PNG whose
    # eXIf chunk sits after IDAT falls through to `load()`, decoding every photo in the library
    # that has no location. Any pixel decode on this path is the bug.
    from PIL import Image as PILImage

    def _boom(self, *args, **kwargs):
        raise AssertionError("the location read decoded pixels")

    monkeypatch.setattr(PILImage.Image, "load", _boom)
    assert (
        _read("png-exif-after-idat.png")
        == ARTEFACT["binaries"]["png-exif-after-idat.png"]["expect"]
    )
    assert _read("jpeg-no-gps.jpg")["location_source"] == location.SOURCE_NONE


def test_a_video_read_touches_only_the_moov_box(tmp_path):
    # The whole reason the reader walks boxes instead of calling ffprobe: 18 GB of household video
    # over NFS must cost a header read each. A clip padded with a megabyte of trailing `free` box
    # must not cost a megabyte of reads — and the meter that proves it is the same one the backfill
    # reports, so this case also pins the counter the pivot trigger is read from.
    source = (BINARIES / "mov-iso6709.mov").read_bytes()
    padded = tmp_path / "padded.mov"
    padded.write_bytes(source + b"\x00\x00\x00\x08free" + b"\x00" * (1 << 20))

    with location.measuring() as meter:
        result = location.read_location(padded, "video/quicktime")
    assert result is not None
    assert (result.latitude, result.longitude) == (40.1, -20.2)
    assert meter.bytes_read < 8192, (
        f"read {meter.bytes_read} bytes of a {padded.stat().st_size}-byte clip"
    )


def test_the_meter_counts_what_an_image_read_costs():
    # Bounded, not exact: Pillow's buffered reader re-reads across its own seeks, so a tiny fixture
    # can be metered at slightly more than its own size. What the number has to prove is that a
    # read costs a HEADER, which is what the backfill's total is judged on.
    with location.measuring() as meter:
        assert location.read_location(BINARIES / "jpeg-north-east.jpg", "image/jpeg") is not None
    assert 0 < meter.bytes_read < 64 * 1024
