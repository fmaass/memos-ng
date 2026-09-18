"""Tests for the one-shot image width/height/blurhash backfill.

Three layers:
* the pure imaging/decision functions (``oriented_size``, ``read_original_size``, ``plan_fields``)
  — asserted against ground truth (the EXIF spec, the BlurHash format) and against the worker's own
  ``process_image``, so "same code path as the worker" is proven per format, not claimed;
* ``load_candidates``/``partition`` — the selection, exercised against the real schema (``adb``)
  per input category: complete rows, each partial-metadata shape, svg, video;
* ``write_fill``/``run_backfill`` — dry-run safety, the per-field fill, its behaviour when another
  writer reaches the row first, the tier fallback, ``--limit``, and idempotency, with the
  attachment rows read back from the database as the authority.
"""

from pathlib import Path

import pytest
from app.config import settings
from app.models import Attachment, AttachmentStatus
from app.scripts import backfill_media_metadata as bf
from app.services import media
from PIL import Image
from sqlalchemy import update

RED = (220, 30, 40)
BLUE = (20, 40, 220)
EXIF_ORIENTATION_TAG = 0x0112  # the EXIF/TIFF tag number, per the spec — not read from the module


def _write_image(
    path: Path, *, fmt: str = "JPEG", size=(400, 300), colour=RED, orientation: int | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, colour)
    if orientation is None:
        img.save(path, fmt)
    else:
        exif = Image.Exif()
        exif[EXIF_ORIENTATION_TAG] = orientation
        img.save(path, fmt, exif=exif)
    return path


def _write_thumb(root: Path, uid: str, *, size=(80, 60), colour=RED) -> Path:
    return _write_image(root / "assets" / "thumbs" / f"{uid}.jpg", size=size, colour=colour)


def _write_mobile(root: Path, uid: str, *, size=(400, 300), colour=RED) -> Path:
    return _write_image(root / "assets" / "mobile" / f"{uid}.jpg", size=size, colour=colour)


def _attachment(uid: str, **kwargs) -> Attachment:
    return Attachment(
        uid=uid,
        filename=f"{uid}.jpg",
        mime=kwargs.pop("mime", "image/jpeg"),
        size=1,
        status=AttachmentStatus.ready,
        **kwargs,
    )


async def _seed(adb, uid: str, **kwargs) -> Attachment:
    att = _attachment(uid, **kwargs)
    adb.add(att)
    await adb.flush()
    return att


async def _write_behind_the_session(adb, attachment_id: int, **values) -> None:
    """Change the row the way another process would.

    ``synchronize_session=False`` is the whole point: an ORM-enabled UPDATE would expire the
    matching attributes, the next SELECT would repopulate them, and the backfill would plan from
    fresh data — which is not the situation under test. The worker runs in its own process, so this
    session must keep the pre-write snapshot it already loaded.
    """
    await adb.execute(
        update(Attachment)
        .where(Attachment.id == attachment_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )


# --- dims: orientation + the worker's own code path --------------------------------------------


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        (1, (400, 300)),  # normal
        (2, (400, 300)),  # mirrored horizontally
        (3, (400, 300)),  # rotated 180
        (4, (400, 300)),  # mirrored vertically
        (5, (300, 400)),  # transposed — axes exchanged
        (6, (300, 400)),  # rotated 90 CW
        (7, (300, 400)),  # transversed
        (8, (300, 400)),  # rotated 90 CCW
    ],
)
def test_oriented_size_exchanges_axes_only_for_orientations_5_to_8(orientation, expected):
    assert bf.oriented_size((400, 300), orientation) == expected


@pytest.mark.parametrize(("fmt", "ext"), [("JPEG", "jpg"), ("TIFF", "tif")])
def test_read_original_size_matches_what_the_worker_records(monkeypatch, tmp_path, fmt, ext):
    """Must agree with process_image's exif_transpose for EVERY container format.

    The two formats disagree about who applies the orientation: Pillow's TIFF loader rotates while
    decoding and then drops tag 274, its JPEG loader touches neither. A rule applied to an
    undecoded image would rotate the TIFF twice, so this pins parity per format, not per rule.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(
        tmp_path / "originals" / f"rot.{ext}", fmt=fmt, size=(400, 300), orientation=6
    )
    att = _attachment(f"rot-{ext}", storage_path=str(src))

    worker_meta = media.process_image(att)

    assert bf.read_original_size(str(src)) == (300, 400)  # EXIF 6 → portrait
    assert bf.read_original_size(str(src)) == (worker_meta["width"], worker_meta["height"])


def test_read_original_size_without_exif_is_the_stored_size(tmp_path):
    src = _write_image(tmp_path / "plain.jpg", size=(640, 480))
    assert bf.read_original_size(str(src)) == (640, 480)


def test_read_tier_size_prefers_mobile_and_returns_none_without_tiers(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    assert bf.read_tier_size("nope") is None
    _write_thumb(tmp_path, "both", size=(80, 60))
    _write_mobile(tmp_path, "both", size=(800, 600))
    assert bf.read_tier_size("both") == (800, 600)  # mobile is the larger tier
    _write_thumb(tmp_path, "thumbonly", size=(60, 80))
    assert bf.read_tier_size("thumbonly") == (60, 80)


# --- blurhash ----------------------------------------------------------------------------------


def test_blurhash_is_deterministic_and_well_formed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _write_thumb(tmp_path, "det", colour=RED)

    first = bf.read_thumb_blurhash("det")
    second = bf.read_thumb_blurhash("det")

    assert first == second
    assert first is not None
    # BlurHash format: 1 size flag + 1 quantised max + 4 DC + 2 per AC component. The default 4x3
    # grid has 11 AC components (28 chars) and its size flag is always 'L'.
    assert len(first) == 28
    assert first[0] == "L"


def test_blurhash_is_taken_from_the_thumb_tier_not_the_original(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _write_thumb(tmp_path, "with-orig", colour=RED)
    _write_image(tmp_path / "originals" / "with-orig.jpg", colour=BLUE)
    _write_thumb(tmp_path, "thumb-only", colour=RED)

    # Same thumb colour, different (or absent) original → the hash must be the same.
    assert bf.read_thumb_blurhash("with-orig") == bf.read_thumb_blurhash("thumb-only")

    _write_thumb(tmp_path, "blue", colour=BLUE)
    assert bf.read_thumb_blurhash("blue") != bf.read_thumb_blurhash("thumb-only")


def test_blurhash_is_none_without_a_thumb_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    assert bf.read_thumb_blurhash("missing") is None


# --- compute -----------------------------------------------------------------------------------


def test_compute_reads_nothing_it_was_not_asked_for(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "c.jpg", size=(200, 100))
    _write_thumb(tmp_path, "c")

    neither = bf.compute("c", str(src), False, False)
    assert neither.size is None and neither.blurhash is None

    dims_only = bf.compute("c", str(src), True, False)
    assert dims_only.size == (200, 100) and dims_only.blurhash is None

    hash_only = bf.compute("c", str(src), False, True)
    assert hash_only.size is None and hash_only.blurhash is not None


def test_compute_falls_back_to_a_tier_when_the_original_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _write_mobile(tmp_path, "gone", size=(600, 800))

    missing = bf.compute("gone", str(tmp_path / "originals" / "gone.jpg"), True, False)
    assert missing.size == (600, 800) and missing.size_from_tier is True

    no_path = bf.compute("gone", None, True, False)
    assert no_path.size == (600, 800) and no_path.size_from_tier is True

    corrupt = tmp_path / "originals" / "corrupt.jpg"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not an image")
    assert bf.compute("gone", str(corrupt), True, False).size_from_tier is True


def test_compute_leaves_size_none_when_nothing_is_readable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    result = bf.compute("nothing", str(tmp_path / "nothing.jpg"), True, True)
    assert result.size is None and result.blurhash is None


# --- per-field fill rule -----------------------------------------------------------------------


def test_plan_fields_never_touches_a_populated_column():
    att = _attachment("full", width=111, height=222, blurhash="LEHV6nWB")
    computed = bf.Computed(size=(999, 888), blurhash="LKO2?U%2")
    assert bf.plan_fields(att, computed) == []


def test_plan_fields_fills_only_the_null_columns():
    att = _attachment("partial", width=None, height=None, blurhash="LEHV6nWB")
    computed = bf.Computed(size=(999, 888), blurhash="LKO2?U%2")
    assert bf.plan_fields(att, computed) == ["width", "height"]


@pytest.mark.parametrize("from_tier", [False, True])
@pytest.mark.parametrize(("width", "height"), [(None, 3000), (512, None)])
def test_dims_are_all_or_nothing_whatever_they_came_from(from_tier, width, height):
    """One stored dimension completed from another source describes neither image.

    Not a tier-only rule: an original's 900px height beside a stored width of 7 stores the ratio
    7:900, and since the media layout batch that ratio IS the tile's box.
    """
    att = _attachment("half", width=width, height=height, blurhash=None)
    computed = bf.Computed(size=(512, 683), size_from_tier=from_tier, blurhash="LKO2?U%2")
    assert bf.plan_fields(att, computed) == ["blurhash"]

    both_null = _attachment("none", width=None, height=None)
    assert bf.plan_fields(both_null, computed) == ["width", "height", "blurhash"]


async def test_write_fill_writes_exactly_the_planned_fields(adb, tmp_path):
    att = await _seed(adb, "apply", width=None, height=None, blurhash="LEHV6nWB")
    computed = bf.Computed(size=(999, 888), blurhash="LKO2?U%2")

    written = await bf.write_fill(adb, att, computed, bf.plan_fields(att, computed))

    assert written == ["width", "height"]
    await adb.refresh(att)
    assert (att.width, att.height, att.blurhash) == (999, 888, "LEHV6nWB")


# --- selection ---------------------------------------------------------------------------------


async def test_load_candidates_covers_every_partial_shape_and_excludes_video(adb):
    complete = await _seed(adb, "sel-complete", width=1, height=2, blurhash="LEHV6nWB")
    all_null = await _seed(adb, "sel-all-null")
    no_width = await _seed(adb, "sel-no-width", height=2, blurhash="LEHV6nWB")
    no_height = await _seed(adb, "sel-no-height", width=1, blurhash="LEHV6nWB")
    no_hash = await _seed(adb, "sel-no-hash", width=1, height=2)
    svg = await _seed(adb, "sel-svg", mime="image/svg+xml")
    await _seed(adb, "sel-video", mime="video/mp4")

    candidates = await bf.load_candidates(adb)

    ids = [att.id for att in candidates]
    assert ids == [all_null.id, no_width.id, no_height.id, no_hash.id, svg.id]
    assert complete.id not in ids  # nothing missing → never a candidate
    eligible, unsupported = bf.partition(candidates)
    assert [att.id for att in eligible] == [all_null.id, no_width.id, no_height.id, no_hash.id]
    assert [att.id for att in unsupported] == [svg.id]  # svg: outside the raster allowlist


def test_partition_keeps_only_the_raster_allowlist():
    """Both sides are literal mime strings: dropping a type from the constant must turn this red."""
    candidates = [
        _attachment("jpeg", mime="image/jpeg"),
        _attachment("png", mime="image/png"),
        _attachment("gif", mime="image/gif"),
        _attachment("webp", mime="image/webp"),
        _attachment("heic", mime="image/heic"),
        _attachment("heif", mime="image/heif"),
        _attachment("tiff", mime="image/tiff"),
        _attachment("bmp", mime="image/bmp"),
        _attachment("svg", mime="image/svg+xml"),
        _attachment("avif", mime="image/avif"),
        _attachment("octet", mime="application/octet-stream"),
        _attachment("weird", mime="image/x-weird"),
    ]

    eligible, unsupported = bf.partition(candidates)

    assert [att.uid for att in eligible] == [
        "jpeg",
        "png",
        "gif",
        "webp",
        "heic",
        "heif",
        "tiff",
        "bmp",
    ]
    assert [att.uid for att in unsupported] == ["svg", "avif", "octet", "weird"]


# --- concurrency: a writer that reaches the row after the candidate load ------------------------


async def test_a_value_written_after_the_candidate_load_is_not_overwritten(
    adb, monkeypatch, tmp_path
):
    """The worker finishes a fresh upload's MediaJob between the candidate query and the write.

    The sequence is the production one: candidates are read once, then each row is planned and
    written minutes later. The plan is formed while the column is genuinely NULL, the worker lands
    in between, and the write must leave its value alone — while still filling the columns that
    really are NULL.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "race.jpg", size=(1200, 900))
    _write_thumb(tmp_path, "race", colour=BLUE)
    att = await _seed(adb, "race", storage_path=str(src))
    worker_hash = "L" + "0" * 27

    assert att in await bf.load_candidates(adb)  # the snapshot this run plans from
    computed = bf.compute("race", str(src), True, True)
    fields = bf.plan_fields(att, computed)
    assert fields == ["width", "height", "blurhash"]

    await _write_behind_the_session(adb, att.id, blurhash=worker_hash)
    written = await bf.write_fill(adb, att, computed, fields)

    await adb.refresh(att)
    assert att.blurhash == worker_hash  # the concurrent writer's value survived
    assert (att.width, att.height) == (1200, 900)  # genuinely NULL columns were still filled
    assert written == ["width", "height"]  # and the run does not claim a column it did not write


async def test_dims_written_after_the_candidate_load_are_not_overwritten(
    adb, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "race2.jpg", size=(1200, 900))
    _write_thumb(tmp_path, "race2")
    att = await _seed(adb, "race2", storage_path=str(src))

    assert att in await bf.load_candidates(adb)
    computed = bf.compute("race2", str(src), True, True)
    fields = bf.plan_fields(att, computed)

    await _write_behind_the_session(adb, att.id, width=4000, height=3000)
    written = await bf.write_fill(adb, att, computed, fields)

    await adb.refresh(att)
    assert (att.width, att.height) == (4000, 3000)  # not replaced by this run's 1200x900
    assert att.blurhash is not None  # the column nobody else touched was still filled
    assert written == ["blurhash"]


async def test_original_dims_are_not_paired_with_a_concurrently_written_dimension(
    adb, monkeypatch, tmp_path
):
    """The all-or-nothing rule holds in SQL for original-derived dims too, not only tier ones.

    The snapshot says both columns are NULL, so the pair is planned; a writer lands one of them
    before the write. Completing the other per-column would store 4000x900 — neither the original's
    ratio nor the concurrent writer's.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "race4.jpg", size=(1200, 900))
    att = await _seed(adb, "race4", storage_path=str(src))

    assert att in await bf.load_candidates(adb)
    computed = bf.compute("race4", str(src), True, False)
    assert computed.size_from_tier is False
    fields = bf.plan_fields(att, computed)
    assert fields == ["width", "height"]

    await _write_behind_the_session(adb, att.id, width=4000)
    written = await bf.write_fill(adb, att, computed, fields)

    await adb.refresh(att)
    assert (att.width, att.height) == (4000, None)
    assert written == []


async def test_tier_dims_are_not_paired_with_a_concurrently_written_dimension(
    adb, monkeypatch, tmp_path
):
    """The all-or-nothing tier rule has to hold in SQL too, not just against the snapshot."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _write_mobile(tmp_path, "race3", size=(600, 800))
    att = await _seed(adb, "race3", storage_path=str(tmp_path / "originals" / "gone.jpg"))

    assert att in await bf.load_candidates(adb)
    computed = bf.compute("race3", None, True, False)
    assert computed.size_from_tier is True
    fields = bf.plan_fields(att, computed)
    assert fields == ["width", "height"]

    await _write_behind_the_session(adb, att.id, height=3000)
    written = await bf.write_fill(adb, att, computed, fields)

    await adb.refresh(att)
    assert (att.width, att.height) == (None, 3000)  # a 600px tier width beside 3000 would lie
    assert written == []


# --- the run -----------------------------------------------------------------------------------


async def test_dry_run_reports_the_plan_and_writes_nothing(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "dry.jpg", size=(1200, 900))
    _write_thumb(tmp_path, "dry")
    att = await _seed(adb, "dry", storage_path=str(src))
    await _seed(adb, "dry-svg", mime="image/svg+xml")

    stats = await bf.run_backfill(adb, execute=False)

    assert (att.width, att.height, att.blurhash) == (None, None, None)  # untouched
    assert stats.candidates == 2 and stats.skipped_unsupported_mime == 1
    assert stats.processed == 1
    assert stats.dims_filled == 1 and stats.blurhash_filled == 1
    assert stats.dims_from_tier == 0 and stats.failed == 0
    assert "DRY RUN" in stats.format(execute=False)


async def test_execute_fills_null_metadata_and_a_second_run_has_nothing_to_do(
    adb, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "run.jpg", size=(1200, 900), orientation=6)
    _write_thumb(tmp_path, "run")
    att = await _seed(adb, "run", storage_path=str(src))

    first = await bf.run_backfill(adb, execute=True)

    assert first.dims_filled == 1 and first.blurhash_filled == 1
    assert (att.width, att.height) == (900, 1200)  # EXIF 6 → portrait, as the worker would record
    assert att.blurhash is not None and len(att.blurhash) == 28

    second = await bf.run_backfill(adb, execute=True)

    assert second.candidates == 0 and second.processed == 0
    assert second.dims_filled == 0 and second.blurhash_filled == 0


async def test_execute_respects_an_existing_value_and_fills_the_rest(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "keep.jpg", size=(1200, 900))
    _write_thumb(tmp_path, "keep")
    att = await _seed(adb, "keep", storage_path=str(src), blurhash="LEHV6nWBaaaaaaaaaaaa")

    stats = await bf.run_backfill(adb, execute=True)

    assert (att.width, att.height) == (1200, 900)  # the NULL pair was filled
    assert att.blurhash == "LEHV6nWBaaaaaaaaaaaa"  # stored value wins over the re-encoded one
    assert stats.dims_filled == 1 and stats.blurhash_filled == 0


async def test_execute_never_completes_a_half_populated_dimension_pair(adb, monkeypatch, tmp_path):
    """A stored width of 7 beside a 1200x900 original must NOT become 7x900.

    The original-derived path filled the columns independently, so this row left the backfill with
    an aspect ratio of 0.008 — which the feed then lays the photo's tile out at. The pair is left
    exactly as found and reported, while the blurhash (no pairing) is still filled.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_image(tmp_path / "originals" / "half.jpg", size=(1200, 900))
    _write_thumb(tmp_path, "half")
    att = await _seed(adb, "half", storage_path=str(src), width=7)

    stats = await bf.run_backfill(adb, execute=True)

    assert (att.width, att.height) == (7, None)
    assert att.blurhash is not None and len(att.blurhash) == 28
    assert stats.skipped_inconsistent_dims == 1
    assert stats.dims_filled == 0 and stats.dims_unresolved == 0
    assert stats.blurhash_filled == 1
    assert "skipped_inconsistent_dims=1" in stats.format(execute=True)


async def test_execute_falls_back_to_the_tier_and_counts_it_separately(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _write_mobile(tmp_path, "tier", size=(600, 800))
    _write_thumb(tmp_path, "tier")
    att = await _seed(adb, "tier", storage_path=str(tmp_path / "originals" / "gone.jpg"))

    stats = await bf.run_backfill(adb, execute=True)

    assert (att.width, att.height) == (600, 800)
    assert stats.dims_filled == 1 and stats.dims_from_tier == 1


async def test_unresolvable_attachment_is_counted_not_written(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _seed(adb, "lost", storage_path=str(tmp_path / "originals" / "lost.jpg"))

    stats = await bf.run_backfill(adb, execute=True)

    assert (att.width, att.height, att.blurhash) == (None, None, None)
    assert stats.dims_unresolved == 1 and stats.blurhash_unresolved == 1
    assert stats.dims_filled == 0 and stats.failed == 0


async def test_unsupported_mime_is_never_written(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _write_thumb(tmp_path, "svg-run")
    svg = await _seed(adb, "svg-run", mime="image/svg+xml")

    stats = await bf.run_backfill(adb, execute=True)

    assert (svg.width, svg.height, svg.blurhash) == (None, None, None)
    assert stats.skipped_unsupported_mime == 1 and stats.processed == 0


async def test_limit_caps_the_number_of_attachments_processed(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    seeded = []
    for i in range(3):
        src = _write_image(tmp_path / "originals" / f"lim{i}.jpg", size=(100 + i, 50))
        _write_thumb(tmp_path, f"lim{i}")
        seeded.append(await _seed(adb, f"lim{i}", storage_path=str(src)))

    stats = await bf.run_backfill(adb, execute=True, limit=1)

    assert stats.candidates == 3 and stats.processed == 1
    assert seeded[0].width == 100  # oldest first
    assert (seeded[1].width, seeded[2].width) == (None, None)
