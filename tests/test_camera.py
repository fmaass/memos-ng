"""`read_camera`: the eight camera tags out of an image's own EXIF, only safe values (MEMNG-865).

Two contracts are held here. The OUTCOME contract — a file read and silent is `None` ('none'), a
file that could not be read is `UNREADABLE` ('unreadable'), a non-image is never read
('not-image') — is the backfill's resume ledger, so flattening any two of them would either
re-read the library forever or write a dead mount off as "no camera". The VALUE contract (fold F1)
is the one guard between a file's bytes and an SQL parameter: every field is validated on its own,
and an invalid one becomes None without taking a valid sibling with it.

The images are built per test by `tests/camera_fixtures.py`.
"""

import math
from fractions import Fraction

import pytest
from app.services import camera
from PIL.TiffImagePlugin import IFDRational

from tests import camera_fixtures as fx


def _fields(info: camera.CameraInfo) -> dict[str, object]:
    return {
        "make": info.make,
        "model": info.model,
        "lens_model": info.lens_model,
        "f_number": info.f_number,
        "exposure_time_s": info.exposure_time_s,
        "focal_length_mm": info.focal_length_mm,
        "exposure_bias_ev": info.exposure_bias_ev,
        "iso": info.iso,
    }


def _approx(expected: dict[str, object]) -> dict[str, object]:
    return {k: pytest.approx(v) if isinstance(v, float) else v for k, v in expected.items()}


# --- outcomes -------------------------------------------------------------------------------


def test_a_jpeg_with_full_exif_yields_all_eight_fields(tmp_path):
    found = camera.read_camera(fx.full_jpeg(tmp_path / "full.jpg"), "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert _fields(found) == _approx(fx.FULL)
    assert camera.source_of("image/jpeg", found) == camera.SOURCE_EXIF


def test_a_heif_yields_make_and_model(tmp_path):
    found = camera.read_camera(fx.make_model_heic(tmp_path / "p.heic"), "image/heic")
    assert isinstance(found, camera.CameraInfo)
    assert (found.make, found.model) == ("Pomme", "Phone 16")
    assert camera.source_of("image/heic", found) == camera.SOURCE_EXIF


def test_a_heif_whose_headers_cannot_address_its_exif_falls_back_instead_of_saying_none(
    tmp_path, monkeypatch
):
    # The box walker returning None means "not answerable from the headers", never "no Exif"
    # (capture.heif_exif_from): the full open must still find the camera.
    path = fx.make_model_heic(tmp_path / "p.heic")
    monkeypatch.setattr(camera.capture, "heif_exif_from", lambda f: None)
    found = camera.read_camera(path, "image/heic")
    assert isinstance(found, camera.CameraInfo)
    assert (found.make, found.model) == ("Pomme", "Phone 16")


def test_an_image_without_exif_yields_none_and_the_none_class(tmp_path):
    found = camera.read_camera(fx.bare_jpeg(tmp_path / "bare.jpg"), "image/jpeg")
    assert found is None
    assert camera.source_of("image/jpeg", found) == camera.SOURCE_NONE


def test_exif_with_no_camera_tag_is_none(tmp_path):
    found = camera.read_camera(fx.dated_only_jpeg(tmp_path / "d.jpg"), "image/jpeg")
    assert found is None


def test_a_corrupt_file_is_unreadable_not_null_as_success(tmp_path):
    found = camera.read_camera(fx.corrupt(tmp_path / "bad.jpg"), "image/jpeg")
    assert found is camera.UNREADABLE
    assert camera.source_of("image/jpeg", found) == camera.SOURCE_UNREADABLE


@pytest.mark.parametrize(
    ("builder", "mime"),
    [
        (fx.truncated_ifd_jpeg, "image/jpeg"),  # IFD0 declares an entry it does not hold
        (fx.dangling_sub_ifd_jpeg, "image/jpeg"),  # the Exif sub-IFD lies past the end
        (fx.dangling_value_jpeg, "image/jpeg"),  # a value offset lies past the end
        (fx.truncated_ifd_heic, "image/heic"),  # the same damage, reached by the box walker
    ],
)
def test_a_structurally_broken_exif_is_unreadable_never_none(builder, mime, tmp_path):
    # Pillow answers these with a WARNING and a short directory, not an exception; a reader that
    # trusted the silence would settle them as 'none', which no run ever visits again.
    found = camera.read_camera(builder(tmp_path / "broken"), mime)
    assert found is camera.UNREADABLE


def test_a_broken_heif_exif_is_unreadable_on_the_fallback_open_too(tmp_path, monkeypatch):
    path = fx.truncated_ifd_heic(tmp_path / "p.heic")
    monkeypatch.setattr(camera.capture, "heif_exif_from", lambda f: None)
    assert camera.read_camera(path, "image/heic") is camera.UNREADABLE


def test_a_tiff_reads_its_camera_from_its_own_directories(tmp_path):
    found = camera.read_camera(fx.tiff_with_camera(tmp_path / "t.tif"), "image/tiff")
    assert isinstance(found, camera.CameraInfo)
    assert _fields(found) == _approx(fx.FULL)


def test_a_missing_original_is_unreadable(tmp_path):
    found = camera.read_camera(tmp_path / "gone.jpg", "image/jpeg")
    assert found is camera.UNREADABLE


def test_a_video_is_not_read_and_classifies_as_not_image(tmp_path):
    # The path does not exist: had the reader opened it, the answer would be UNREADABLE.
    found = camera.read_camera(tmp_path / "clip.mp4", "video/mp4")
    assert found is None
    assert camera.source_of("video/mp4", found) == camera.SOURCE_NOT_IMAGE
    assert camera.source_of("application/pdf", None) == camera.SOURCE_NOT_IMAGE


def test_the_ledger_values_fit_the_column():
    for value in (
        camera.SOURCE_EXIF,
        camera.SOURCE_NONE,
        camera.SOURCE_UNREADABLE,
        camera.SOURCE_NOT_IMAGE,
    ):
        assert len(value) <= 16


# --- F1: per-field validation inside the reader ---------------------------------------------


def test_an_iso_beyond_postgres_integer_is_dropped_and_the_siblings_are_kept(tmp_path):
    found = camera.read_camera(fx.huge_iso_jpeg(tmp_path / "iso.jpg"), "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert found.iso is None
    assert _fields(found) == _approx({**fx.FULL, "iso": None})


def test_an_fnumber_with_a_zero_denominator_is_dropped(tmp_path):
    found = camera.read_camera(fx.zero_denominator_jpeg(tmp_path / "f0.jpg"), "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert found.f_number is None
    assert _fields(found) == _approx({**fx.FULL, "f_number": None})


def test_a_nul_inside_the_make_is_removed(tmp_path):
    found = camera.read_camera(fx.nul_make_jpeg(tmp_path / "nul.jpg"), "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert found.make == "ACMEBODY"


def test_a_negative_bias_keeps_its_sign(tmp_path):
    found = camera.read_camera(fx.full_jpeg(tmp_path / "full.jpg"), "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert found.exposure_bias_ev == pytest.approx(-0.3)


def test_a_file_whose_only_camera_field_is_invalid_is_none(tmp_path):
    found = camera.read_camera(fx.invalid_only_jpeg(tmp_path / "inv.jpg"), "image/jpeg")
    assert found is None
    assert camera.source_of("image/jpeg", found) == camera.SOURCE_NONE


def test_utf8_bytes_decode_and_invalid_bytes_are_replaced(tmp_path):
    path = fx.jpeg(tmp_path / "u.jpg", {fx.MAKE: "Löwe".encode(), fx.MODEL: b"X\xff1"}, {})
    found = camera.read_camera(path, "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert found.make == "Löwe"
    assert found.model == "X�1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  ACME  ", "ACME"),
        ("ACME\x00BODY", "ACMEBODY"),
        ("A\x01B\x1fC\x7fD\x85E\x9fF", "ABCDEF"),  # C0, DEL and C1
        ("\x00\x00 \t ", None),  # empty after cleaning
        ("", None),
        (b"Nikon\x00", "Nikon"),
        ("x" * 300, "x" * 255),
        ("\ud800bad", "bad"),  # a lone surrogate cannot be encoded for Postgres
        (42, None),
        (None, None),
        (("tuple",), None),
    ],
)
def test_clean_text(raw, expected):
    assert camera.clean_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "low", "high", "low_inclusive", "expected"),
    [
        (IFDRational(28, 10), 0.0, 1000.0, False, 2.8),
        (IFDRational(28, 0), 0.0, 1000.0, False, None),  # zero denominator
        (Fraction(1, 250), 0.0, 86400.0, False, 0.004),
        (0, 0.0, 1000.0, False, None),  # the open bound is open
        (1000, 0.0, 1000.0, False, 1000.0),  # the closed bound is closed
        (1000.5, 0.0, 1000.0, False, None),
        (-100, -100.0, 100.0, True, -100.0),
        (-100.01, -100.0, 100.0, True, None),
        (float("inf"), 0.0, 1000.0, False, None),
        (float("nan"), 0.0, 1000.0, False, None),
        (10**400, 0.0, 1000.0, False, None),  # OverflowError
        ("abc", 0.0, 1000.0, False, None),  # ValueError
        ((1, 2), 0.0, 1000.0, False, None),  # TypeError
        (True, 0.0, 1000.0, False, None),
        (None, 0.0, 1000.0, False, None),
    ],
)
def test_clean_number(raw, low, high, low_inclusive, expected):
    got = camera.clean_number(raw, low, high, low_inclusive=low_inclusive)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)
        assert math.isfinite(got)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (400, 400),
        (1, 1),
        (0, None),
        (2147483647, 2147483647),
        (2147483648, None),
        (4294967295, None),
        (-5, None),
        ((800, 0), 800),  # ISOSpeedRatings may carry several values; the first is the ISO
        ((), None),
        (100.0, 100),
        (100.5, None),  # not integral
        (IFDRational(0, 0), None),
        (True, None),
        ("400", None),
        (None, None),
    ],
)
def test_clean_iso(raw, expected):
    assert camera.clean_iso(raw) == expected


def test_each_number_range_matches_the_fold():
    # One out-of-range value per numeric field, each next to a valid sibling it must not discard.
    sub = {
        fx.F_NUMBER: IFDRational(1001, 1),
        fx.EXPOSURE_TIME: IFDRational(86401, 1),
        fx.FOCAL_LENGTH: IFDRational(10001, 1),
        fx.EXPOSURE_BIAS: IFDRational(101, 1),
        fx.ISO: 400,
    }
    found = camera.camera_from_tags({fx.MAKE: "ACME"}, sub)
    assert found == camera.CameraInfo(make="ACME", iso=400)


def test_concurrent_reads_classify_each_file_by_its_own_directories(tmp_path):
    # The structural signal lives on each read's own directory object, not in the process-wide
    # warnings state, so interleaved reads on many threads cannot borrow each other's verdict.
    from concurrent.futures import ThreadPoolExecutor

    good = fx.full_jpeg(tmp_path / "good.jpg")
    broken = fx.truncated_ifd_jpeg(tmp_path / "broken.jpg")
    jobs = [good if i % 2 else broken for i in range(64)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda p: camera.read_camera(p, "image/jpeg"), jobs))
    for path, found in zip(jobs, results, strict=True):
        if path == good:
            assert isinstance(found, camera.CameraInfo) and found.make == "ACME"
        else:
            assert found is camera.UNREADABLE


@pytest.mark.parametrize(
    ("mode", "big"),
    [("RGB", False), ("I;16B", False), ("RGB", True), ("I;16B", True)],
    ids=["classic-II", "classic-MM", "bigtiff-II", "bigtiff-MM"],
)
def test_every_tiff_header_form_reads_its_camera_or_says_none(mode, big, tmp_path):
    tagged = fx.tiff_file(tmp_path / "t.tif", mode=mode, big=big, tagged=True)
    head = tagged.read_bytes()[:4]  # the fixture really is the header form it claims
    assert head[:2] == (b"MM" if mode == "I;16B" else b"II")
    assert int.from_bytes(head[2:4], "big" if head[:2] == b"MM" else "little") == (
        43 if big else 42
    )
    if mode == "I;16B" and big:
        # A known limit, not a verdict on the file: Pillow 12 detects BigTIFF from byte 2 only, so
        # it cannot open a big-endian one at all, and the reader settles it as retryable.
        assert camera.read_camera(tagged, "image/tiff") is camera.UNREADABLE
        return
    found = camera.read_camera(tagged, "image/tiff")
    assert isinstance(found, camera.CameraInfo)
    assert (found.make, found.model) == ("ACME", "M")
    untagged = fx.tiff_file(tmp_path / "u.tif", mode=mode, big=big, tagged=False)
    assert camera.read_camera(untagged, "image/tiff") is None


def test_a_big_endian_exif_payload_reads(tmp_path):
    found = camera.read_camera(fx.big_endian_exif_jpeg(tmp_path / "mm.jpg"), "image/jpeg")
    assert isinstance(found, camera.CameraInfo)
    assert _fields(found) == _approx(fx.FULL)


@pytest.mark.parametrize(("bytesize", "reserved"), [(4, 0), (8, 1)])
def test_a_bigtiff_header_the_format_does_not_allow_is_unreadable(bytesize, reserved, tmp_path):
    path = fx.bad_bigtiff_header(tmp_path / "b.tif", bytesize=bytesize, reserved=reserved)
    assert camera.read_camera(path, "image/tiff") is camera.UNREADABLE


def test_a_png_reads_its_camera_from_the_exif_chunk(tmp_path):
    from PIL import Image as PILImage

    path = tmp_path / "c.png"
    PILImage.new("RGB", (8, 8)).save(path, "PNG", exif=fx.exif_bytes(*fx.full_tags()))
    found = camera.read_camera(path, "image/png")
    assert isinstance(found, camera.CameraInfo)
    assert _fields(found) == _approx(fx.FULL)


# --- a payload a container DECLARES but cannot complete (final audit, fold 3) -----------------
#
# The rule: a container that declares EXIF and whose payload cannot yield a complete header and a
# complete IFD0 is a failed read — 'unreadable', which `--retry-unreadable` revisits — never
# 'none', which is final. Absent EXIF and a complete, empty IFD0 stay 'none'. Every member of the
# class is pinned on every path a payload reaches the reader by.

P = fx.EXIF_PREFIX


def _members(prefix: bytes) -> list[tuple[str, bytes]]:
    """Every truncation of a classic and a BigTIFF header, a dangling IFD0, a doubled prefix."""
    tag = "prefix+" if prefix else ""
    found = [(f"{tag}classic[:{k}]", prefix + fx.EMPTY_CLASSIC[:k]) for k in range(9)]
    found += [(f"{tag}bigtiff[:{k}]", prefix + fx.EMPTY_BIG[:k]) for k in range(4, 17)]
    found.append((f"{tag}ifd0-past-end", prefix + fx.PAST_END))
    if prefix:
        found.append(("prefix+prefix", prefix + prefix))
    return found


def _heif_headers(path, payload):
    return fx.heif_exif_extent(path, payload)


# (builder, mime, suffix, whether the carrier can hold a payload WITHOUT the APP1 prefix)
CARRIERS = {
    "jpeg-app1": (fx.jpeg_app1, "image/jpeg", ".jpg", False),
    "png-before-idat": (
        lambda p, b: fx.png_exif_chunk(p, b, after_idat=False),
        "image/png",
        ".png",
        True,
    ),
    "png-after-idat": (
        lambda p, b: fx.png_exif_chunk(p, b, after_idat=True),
        "image/png",
        ".png",
        True,
    ),
    "webp-chunk": (fx.webp_exif_chunk, "image/webp", ".webp", True),
    "heif-headers": (_heif_headers, "image/heic", ".heic", True),
    "heif-fallback": (_heif_headers, "image/heic", ".heic", True),
}

INCOMPLETE = [
    pytest.param(carrier, payload, id=f"{carrier}-{label}")
    for carrier, (_b, _m, _s, bare) in CARRIERS.items()
    for prefix in ((P, b"") if bare else (P,))
    for label, payload in _members(prefix)
]

COMPLETE_AND_EMPTY = [
    pytest.param(carrier, prefix + body, id=f"{carrier}-{'prefix+' if prefix else ''}{name}")
    for carrier, (_b, _m, _s, bare) in CARRIERS.items()
    for prefix in ((P, b"") if bare else (P,))
    for name, body in (("classic", fx.EMPTY_CLASSIC), ("bigtiff", fx.EMPTY_BIG))
]


def _read_through(carrier: str, payload: bytes, tmp_path, monkeypatch) -> camera.CameraRead:
    build, mime, suffix, _bare = CARRIERS[carrier]
    path = build(tmp_path / f"declared{suffix}", payload)
    if carrier == "heif-fallback":
        # The full open the box walk falls back to — the same file, read by libheif.
        monkeypatch.setattr(camera.capture, "heif_exif_from", lambda f: None)
    return camera.read_camera(path, mime)


@pytest.mark.parametrize(("carrier", "payload"), INCOMPLETE)
def test_a_declared_payload_without_a_complete_header_and_ifd0_is_unreadable(
    carrier, payload, tmp_path, monkeypatch
):
    assert _read_through(carrier, payload, tmp_path, monkeypatch) is camera.UNREADABLE


@pytest.mark.parametrize(("carrier", "payload"), COMPLETE_AND_EMPTY)
def test_a_complete_header_with_an_empty_ifd0_is_still_none(
    carrier, payload, tmp_path, monkeypatch
):
    assert _read_through(carrier, payload, tmp_path, monkeypatch) is None


@pytest.mark.parametrize(
    "payload",
    [pytest.param(fx.EMPTY_CLASSIC[:k], id=f"classic[:{k}]") for k in range(9)]
    + [pytest.param(fx.EMPTY_BIG[:k], id=f"bigtiff[:{k}]") for k in range(4, 17)]
    + [pytest.param(fx.PAST_END, id="ifd0-past-end")],
)
def test_a_tiff_file_that_stops_inside_its_header_or_ifd0_is_unreadable(payload, tmp_path):
    path = tmp_path / "short.tif"
    path.write_bytes(payload)
    assert camera.read_camera(path, "image/tiff") is camera.UNREADABLE


@pytest.mark.parametrize("payload", [b"", fx.EMPTY_CLASSIC], ids=["empty", "no-identifier"])
def test_an_app1_segment_without_the_exif_identifier_declares_no_exif(payload, tmp_path):
    # JPEG names its EXIF by the `Exif\0\0` identifier alone; any other APP1 (XMP, or nothing)
    # is not a declaration, so the file is read and silent.
    path = fx.jpeg_app1(tmp_path / "app1.jpg", payload)
    assert camera.read_camera(path, "image/jpeg") is None


@pytest.mark.parametrize(
    ("build", "mime", "suffix"),
    [
        (lambda p: fx.bare_jpeg(p), "image/jpeg", ".jpg"),
        (lambda p: _plain(p, "PNG"), "image/png", ".png"),
        (lambda p: _plain(p, "WEBP"), "image/webp", ".webp"),
        (lambda p: _plain_heic(p), "image/heic", ".heic"),
    ],
    ids=["jpeg", "png", "webp", "heic"],
)
def test_a_container_that_declares_no_exif_is_none(build, mime, suffix, tmp_path):
    assert camera.read_camera(build(tmp_path / f"plain{suffix}"), mime) is None


def _plain(path, kind):
    from PIL import Image as PILImage

    PILImage.new("RGB", (8, 8)).save(path, kind)
    return path


def _plain_heic(path):
    import pillow_heif
    from PIL import Image as PILImage

    pillow_heif.from_pillow(PILImage.new("RGB", (8, 8))).save(path, quality=50)
    return path


@pytest.mark.parametrize(
    ("build", "declared"),
    [
        (lambda p: fx.make_model_heic(p), True),
        (lambda p: fx.heif_exif_extent(p, b""), True),
        (lambda p: _plain_heic(p), False),
        (lambda p: fx.bare_jpeg(p), False),
    ],
    ids=["exif", "empty-exif-item", "no-exif-item", "not-heif"],
)
def test_heif_declares_exif_answers_from_the_item_table_alone(build, declared, tmp_path):
    with open(build(tmp_path / "f.heic"), "rb") as f:
        assert camera.capture.heif_declares_exif(f) is declared


# --- declarations the reader never got a payload for (final audit, fold 4) ------------------


def test_an_oversized_png_exif_after_the_image_data_is_unreadable(tmp_path):
    # Over capture's read cap the chunk is declined, and after IDAT Pillow never sees it:
    # declining to read a declared chunk is not proof it is absent.
    path = fx.png_exif_chunk(tmp_path / "big.png", fx.oversized_exif(), after_idat=True)
    assert camera.read_camera(path, "image/png") is camera.UNREADABLE


def test_the_same_oversized_png_exif_before_the_image_data_still_reads(tmp_path):
    path = fx.png_exif_chunk(tmp_path / "big.png", fx.oversized_exif(), after_idat=False)
    found = camera.read_camera(path, "image/png")
    assert isinstance(found, camera.CameraInfo)
    assert found.make == "ACME"


@pytest.mark.parametrize(("flag", "expected"), [(True, camera.UNREADABLE), (False, None)])
def test_a_vp8x_exif_flag_without_its_chunk_is_unreadable(flag, expected, tmp_path):
    path = fx.webp_vp8x_without_exif_chunk(tmp_path / "f.webp", flag=flag)
    assert camera.read_camera(path, "image/webp") is expected


def test_bytes_past_the_riff_end_are_never_a_declaration(tmp_path):
    path = fx.webp_with_exif_bytes_appended(tmp_path / "f.webp")
    assert camera.read_camera(path, "image/webp") is None


def test_an_empty_exif_chunk_behind_an_odd_sized_chunk_is_found(tmp_path):
    path = fx.webp_odd_chunk_then_empty_exif(tmp_path / "f.webp")
    assert camera.read_camera(path, "image/webp") is camera.UNREADABLE


def _riff(chunks: bytes, *, size: int | None = None, trailing: bytes = b"") -> bytes:
    body = b"WEBP" + chunks
    return b"RIFF" + (len(body) if size is None else size).to_bytes(4, "little") + body + trailing


def _chunk(kind: bytes, data: bytes, *, size: int | None = None) -> bytes:
    declared = len(data) if size is None else size
    return kind + declared.to_bytes(4, "little") + data + b"\x00" * (len(data) % 2)


@pytest.mark.parametrize(
    ("raw", "declared"),
    [
        pytest.param(_riff(_chunk(b"ABCD", b"xyz") + _chunk(b"EXIF", b"")), True, id="after-pad"),
        pytest.param(
            _riff(_chunk(b"ABCD", b"xyz"), trailing=_chunk(b"EXIF", b"")),
            False,
            id="exif-past-riff-end",
        ),
        pytest.param(
            _riff(_chunk(b"ABCD", b"xyz", size=4096) + _chunk(b"EXIF", b"")),
            False,
            id="chunk-overruns-riff-end",
        ),
        pytest.param(_riff(_chunk(b"EXIF", b"", size=4096)), True, id="exif-header-inside"),
        pytest.param(_riff(_chunk(b"VP8X", b"\x08" + bytes(9))), True, id="vp8x-flag"),
        pytest.param(_riff(_chunk(b"VP8X", bytes(10))), False, id="vp8x-no-flag"),
        pytest.param(
            _riff(_chunk(b"ABCD", b"") + _chunk(b"VP8X", b"\x08" + bytes(9))),
            False,
            id="vp8x-not-first",
        ),
        pytest.param(_riff(b"", size=0, trailing=_chunk(b"EXIF", b"")), False, id="riff-size-0"),
    ],
)
def test_the_webp_walk_reads_only_the_declared_container(raw, declared, tmp_path):
    path = tmp_path / "f.webp"
    path.write_bytes(raw)
    assert camera._webp_declares_exif(path) is declared
