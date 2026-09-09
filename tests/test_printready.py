"""The print-ready rendition tier (MEMNG-49 Phase A): encode profile, source choice, enqueue, job.

The encode profile is the load-bearing part of this feature — a 4096 px JPEG that is not sRGB, or
carries no DPI, or was chroma-subsampled, still LOOKS fine on screen and prints wrong. So every
assertion here decodes the file that was actually written and reads the property back out of it;
none of them re-derives an expected value with the code under test.
"""

import asyncio
import os
import random
import secrets
import struct
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentRendition,
    AttachmentStatus,
    JobKind,
    JobLane,
    JobState,
    MediaJob,
)
from app.services import asset_urls, media, printready
from PIL import Image, ImageCms, ImageFile, ImageStat, JpegImagePlugin
from PIL.TiffImagePlugin import IFDRational
from sqlalchemy import create_engine, select, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

# --- fixtures built in code, never committed as binaries -----------------------------------


def _s15(value: float) -> bytes:
    return struct.pack(">i", round(value * 65536))


def _xyz_tag(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + b"\0" * 4 + _s15(x) + _s15(y) + _s15(z)


def _curve_tag(gamma: float) -> bytes:
    return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", round(gamma * 256))


def _desc_tag(text_value: str) -> bytes:
    raw = text_value.encode("ascii") + b"\0"
    return (
        b"desc"
        + b"\0" * 4
        + struct.pack(">I", len(raw))
        + raw
        + struct.pack(">II", 0, 0)  # unicode language code + length
        + struct.pack(">HB", 0, 0)  # scriptcode code + length
        + b"\0" * 67
    )


def wide_gamut_icc() -> bytes:
    """A real, little-cms-loadable ICC profile with Adobe RGB (1998) primaries and gamma 2.2.

    Built here rather than committed because the alternative was a binary fixture, and because
    `ImageCms.createProfile` can only make sRGB — with no second RGB space there is no way to
    prove the difference between a colour TRANSFORM and a mode change, which is exactly the bug
    this tier exists to avoid (a wide-gamut master printing dull).
    """
    tags = {
        b"desc": _desc_tag("Wide gamut test profile"),
        b"wtpt": _xyz_tag(0.9642, 1.0, 0.8249),  # D50, the ICC PCS illuminant
        b"rXYZ": _xyz_tag(0.6097, 0.3111, 0.0195),  # Adobe RGB primaries, D50-adapted
        b"gXYZ": _xyz_tag(0.2053, 0.6257, 0.0609),
        b"bXYZ": _xyz_tag(0.1492, 0.0632, 0.7448),
        b"rTRC": _curve_tag(2.2),
        b"gTRC": _curve_tag(2.2),
        b"bTRC": _curve_tag(2.2),
        b"cprt": b"text" + b"\0" * 4 + b"memos-ng test\0",
    }
    table = struct.pack(">I", len(tags))
    offset = 128 + 4 + 12 * len(tags)
    body = b""
    for sig, data in tags.items():
        table += sig + struct.pack(">II", offset, len(data))
        padding = (-len(data)) % 4
        body += data + b"\0" * padding
        offset += len(data) + padding
    header = (
        struct.pack(">I", 128 + len(table) + len(body))
        + b"\0" * 4  # preferred CMM
        + struct.pack(">I", 0x02100000)  # ICC 2.1
        + b"mntr"
        + b"RGB "
        + b"XYZ "
        + b"\0" * 12  # creation date
        + b"acsp"
        + b"\0" * 16  # platform, flags, manufacturer, model
        + b"\0" * 8  # attributes
        + struct.pack(">I", 0)  # rendering intent: perceptual
        + _s15(0.9642)
        + _s15(1.0)
        + _s15(0.8249)
        + b"\0" * 4  # creator
        + b"\0" * 16  # profile id
        + b"\0" * 28  # reserved
    )
    assert len(header) == 128
    return header + table + body


def _png(path: Path, size=(40, 30), colour=(200, 90, 40), icc: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, "PNG", icc_profile=icc)
    return path


def _attachment(storage: Path | None, uid: str | None = None, **kwargs) -> Attachment:
    """A transient Attachment — generation reads the row, it does not need one in the DB."""
    return Attachment(
        uid=uid or secrets.token_urlsafe(6),
        filename="photo.png",
        mime="image/png",
        size=1,
        status=AttachmentStatus.ready,
        storage_path=str(storage) if storage is not None else None,
        **kwargs,
    )


def _rendition(storage: Path, variant_key: str, rid: int) -> AttachmentRendition:
    return AttachmentRendition(
        id=rid,
        variant_key=variant_key,
        label=variant_key.partition(":")[2] or None,
        filename=storage.name,
        mime="image/png",
        size=1,
        storage_path=str(storage),
    )


def _decode(path: Path) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        return img


# --- encode profile ------------------------------------------------------------------------


def test_encode_profile_of_an_oversized_source(monkeypatch, tmp_path):
    # AC: 4096 px long edge, JPEG whatever the source was, 300 dpi, sRGB embedded, 4:4:4.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_png(tmp_path / "src.png", size=(4200, 2100)))

    dest = media.process_printready(att)

    assert dest == tmp_path / "assets" / "printready" / f"{att.uid}.jpg"
    out = _decode(dest)
    assert out.format == "JPEG"  # the source was a PNG; a 60 MP PNG blows the 50 MB upload cap
    assert out.size == (4096, 2048)  # long edge exactly 4096, aspect preserved
    # Without a DPI tag, layout software assumes 72 and mis-scales the photo on placement.
    assert out.info.get("dpi") == (300, 300)
    icc = out.info.get("icc_profile")
    assert icc, "no colour profile embedded — the print service cannot know the space"
    assert "sRGB" in ImageCms.getProfileDescription(ImageCms.getOpenProfile(BytesIO(icc)))
    assert JpegImagePlugin.get_sampling(out) == 0  # 4:4:4 — chroma artefacts show on gloss paper


def test_a_small_source_is_never_upscaled(monkeypatch, tmp_path):
    # Upscaling invents detail and prints worse than the honest smaller image.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_png(tmp_path / "small.png", size=(800, 600)))

    out = _decode(media.process_printready(att))

    assert out.size == (800, 600)
    assert out.format == "JPEG" and out.info.get("dpi") == (300, 300)
    assert out.info.get("icc_profile")  # a small print file is still a print file


def test_orientation_is_applied_like_the_other_tiers(monkeypatch, tmp_path):
    # A phone portrait is stored landscape plus an EXIF orientation; printing the stored shape
    # would rotate the photo on the page.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    Image.new("RGB", (40, 20), (10, 20, 30)).save(src, "JPEG", exif=exif)
    att = _attachment(src)

    assert _decode(media.process_printready(att)).size == (20, 40)


def test_a_wide_gamut_source_is_colour_transformed_not_just_mode_converted(monkeypatch, tmp_path):
    # The mutation this catches: `.convert("RGB")` instead of a managed transform. It changes the
    # pixel MODE and leaves the numbers alone, so an Adobe RGB master prints dull — the exact
    # failure the plan names. Under a real transform the same numbers must MOVE.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    colour = (200, 90, 40)
    wide = _attachment(_png(tmp_path / "wide.png", colour=colour, icc=wide_gamut_icc()))
    tagged_srgb = _attachment(
        _png(
            tmp_path / "srgb.png",
            colour=colour,
            icc=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
        )
    )

    from_wide = _decode(media.process_printready(wide)).getpixel((0, 0))
    from_srgb = _decode(media.process_printready(tagged_srgb)).getpixel((0, 0))

    assert isinstance(from_wide, tuple) and isinstance(from_srgb, tuple)
    # sRGB in → sRGB out is an identity transform: the numbers survive (±JPEG rounding).
    assert all(abs(a - b) <= 3 for a, b in zip(from_srgb, colour, strict=True))
    # Adobe RGB in → sRGB out re-maps the gamut: the saturated red moves a long way.
    assert from_wide[0] - colour[0] > 15, f"no colour transform happened: {from_wide}"


def test_an_unreadable_profile_falls_back_to_srgb_instead_of_failing(monkeypatch, tmp_path):
    # A corrupt ICC must not cost the photo its print rendition — assume sRGB (what every viewer
    # does) and say so in the log.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_png(tmp_path / "junk.png", icc=b"not-an-icc-profile"))

    with capture_logs() as logs:
        out = _decode(media.process_printready(att))

    assert any(entry["event"] == "printready_icc_unreadable" for entry in logs)
    assert out.info.get("dpi") == (300, 300) and out.info.get("icc_profile")
    pixel = out.getpixel((0, 0))
    assert isinstance(pixel, tuple)
    assert all(abs(a - b) <= 3 for a, b in zip(pixel, (200, 90, 40), strict=True))


def test_the_capture_date_is_stamped_onto_the_rendition(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    captured = datetime(2019, 7, 4, 12, 30, tzinfo=UTC)
    att = _attachment(_png(tmp_path / "dated.png"), captured_at=captured)

    dest = media.process_printready(att)

    assert dest.stat().st_mtime == pytest.approx(captured.timestamp(), abs=1)


# --- which file the rendition is made FROM --------------------------------------------------


def test_the_print_master_wins_over_the_original(monkeypatch, tmp_path):
    # The print rendition is the user's own export for print (crop + output sharpening); deriving
    # from the original would silently throw that work away.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    original = _png(tmp_path / "orig.png", colour=(220, 20, 20))
    master = _png(tmp_path / "print.png", colour=(20, 20, 220))
    att = _attachment(original)
    att.renditions = [_rendition(master, "print:glossy", 7)]

    pixel = _decode(media.process_printready(att)).getpixel((0, 0))

    assert isinstance(pixel, tuple) and pixel[2] > pixel[0]  # blue master, not the red original


def test_the_original_is_used_when_there_is_no_print_master(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_png(tmp_path / "orig.png", colour=(220, 20, 20)))

    pixel = _decode(media.process_printready(att)).getpixel((0, 0))

    assert isinstance(pixel, tuple) and pixel[0] > pixel[2]


def test_print_master_choice_is_deterministic_and_ignores_other_variants(tmp_path):
    att = _attachment(tmp_path / "orig.png")
    matte = _rendition(tmp_path / "matte.png", "print:matte", 3)
    glossy = _rendition(tmp_path / "glossy.png", "print:glossy", 9)
    bare = _rendition(tmp_path / "bare.png", "print", 5)
    other = _rendition(tmp_path / "other.png", "printable", 1)

    att.renditions = [glossy, matte, bare, other]
    assert printready.print_rendition(att) is matte  # lowest id among the print masters
    att.renditions = [other]
    assert printready.print_rendition(att) is None  # `printable` is not a print master
    assert printready.is_print_variant("print") and printready.is_print_variant("print:glossy")
    assert not printready.is_print_variant("printable")
    assert not printready.is_print_variant("screen")


def test_generation_refuses_an_attachment_with_no_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="no source file"):
        media.process_printready(_attachment(None))


# --- the tier as served / swept -------------------------------------------------------------


def _seed_attachment(uid: str, owner_subject: str, mime: str = "image/jpeg") -> int:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        oid = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": owner_subject}
        ).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, filename, mime, size, status, "
                "storage_path) VALUES (:u,:o,'x.jpg',:m,1,CAST('ready' AS attachment_status),'/x') "
                "RETURNING id"
            ),
            {"u": uid, "o": oid, "m": mime},
        ).scalar_one()
    eng.dispose()
    return aid


def _delete_attachments(*ids: int) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in ids:
            c.execute(text("DELETE FROM media_jobs WHERE attachment_id=:a"), {"a": aid})
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


def test_the_tier_is_served_to_its_owner_and_is_not_a_signed_tier(
    logged_in_client, monkeypatch, tmp_path
):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid = _seed_attachment("pr-serve", subject)
    try:
        rendition = printready.printready_path("pr-serve")
        rendition.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 10), (5, 6, 7)).save(rendition, "JPEG", dpi=(300, 300))

        r = client.get("/api/attachments/pr-serve/printready")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"].startswith("private,")
        # Not in the signed/DB-free set: a print master is low fan-out and download-shaped, so it
        # stays on the authenticated path (and no minted URL exposes it by uid alone).
        assert printready.TIER not in asset_urls.DERIVED_TIERS
        assert client.get("/api/attachments/pr-serve/printiness").status_code == 404
    finally:
        _delete_attachments(aid)


def test_deleting_the_photo_removes_its_print_rendition(logged_in_client, monkeypatch, tmp_path):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    aid = _seed_attachment("pr-del", subject)
    try:
        rendition = printready.printready_path("pr-del")
        rendition.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(rendition, "JPEG")

        assert client.delete("/api/attachments/pr-del").status_code == 204

        assert not rendition.exists()  # a 4 MB file per photo would leak otherwise
        assert rendition in media.derived_outputs("pr-del")  # ...and a job's sweep finds it too
    finally:
        _delete_attachments(aid)


# --- enqueue (idempotence) ------------------------------------------------------------------


async def _persisted(
    adb,
    tmp_path,
    *,
    mime="image/png",
    source: Path | None = None,
    renditions: list[AttachmentRendition] | None = None,
) -> Attachment:
    att = Attachment(
        uid=secrets.token_urlsafe(8),
        filename="photo.png",
        mime=mime,
        size=1,
        status=AttachmentStatus.ready,
        storage_path=str(source) if source is not None else str(tmp_path / "missing.png"),
        # Passed at construction, never appended after the flush: appending to an unloaded
        # collection on a persistent row is a lazy load, which async SQLAlchemy refuses outright.
        renditions=renditions or [],
    )
    adb.add(att)
    await adb.flush()
    return att


async def _jobs_for(adb, attachment_id: int) -> list[MediaJob]:
    rows = await adb.execute(select(MediaJob).where(MediaJob.attachment_id == attachment_id))
    return list(rows.scalars())


async def test_enqueue_creates_one_slow_lane_printready_job(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "a.png"))

    result = await printready.enqueue_printready(adb, [att.id])

    assert result.enqueued == (att.id,) and result.fresh == () and result.ineligible == ()
    jobs = await _jobs_for(adb, att.id)
    assert len(jobs) == 1
    assert jobs[0].kind == JobKind.printready  # not the upload pipeline
    assert jobs[0].lane is JobLane.slow and jobs[0].state is JobState.queued


async def test_enqueue_is_a_no_op_while_a_job_is_still_pending(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "b.png"))
    await printready.enqueue_printready(adb, [att.id])

    again = await printready.enqueue_printready(adb, [att.id, att.id])  # duplicate ids too

    assert again.in_flight == (att.id,) and again.enqueued == ()
    assert len(await _jobs_for(adb, att.id)) == 1


async def test_a_terminal_job_does_not_block_a_later_request(adb, monkeypatch, tmp_path):
    # `done`/`failed` are terminal: a finished job must never wedge the photo out of a rebuild,
    # and a failed one has to be retriable by asking again.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "c.png"))
    for state in (JobState.done, JobState.failed):
        adb.add(
            MediaJob(
                attachment_id=att.id, lane=JobLane.slow, kind=JobKind.printready, state=state
            )
        )
    await adb.flush()

    result = await printready.enqueue_printready(adb, [att.id])

    assert result.enqueued == (att.id,)


async def test_a_current_rendition_is_skipped_even_though_its_mtime_was_stamped_back(
    adb, monkeypatch, tmp_path
):
    # THE idempotence pin. Derived tiers carry the photo's CAPTURE date (media._stamp_mtime), so a
    # freshly generated rendition has an mtime from years ago. Compared on mtime alone every
    # rendition reads as stale and a photo book would re-encode the whole library on every export.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = _png(tmp_path / "d.png")
    att = await _persisted(adb, tmp_path, source=source)
    dest = printready.printready_path(att.uid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"rendered")
    stamp = time.time() - 365 * 24 * 3600
    os.utime(dest, (stamp, stamp))

    result = await printready.enqueue_printready(adb, [att.id])

    assert result.fresh == (att.id,) and result.enqueued == ()
    assert await _jobs_for(adb, att.id) == []


async def test_a_rendition_older_than_its_source_is_rebuilt(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=tmp_path / "e.png")
    dest = printready.printready_path(att.uid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"stale")
    await asyncio.sleep(0.01)  # keep the two writes distinguishable in filesystem time
    _png(tmp_path / "e.png")  # the master arrives (or is replaced) after the rendition

    result = await printready.enqueue_printready(adb, [att.id])

    assert result.enqueued == (att.id,)


async def test_a_future_stamped_rendition_cannot_mask_a_replaced_source(
    adb, monkeypatch, tmp_path
):
    # The other direction of the stamp problem: capture time descends from the client-supplied
    # upload last_modified, which nothing bounds — a broken client clock stamps the rendition
    # YEARS ahead. If freshness trusted mtime, that rendition would read "fresh" forever and a
    # replaced master would keep shipping the old crop into a printed book.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=tmp_path / "f.png")
    dest = printready.printready_path(att.uid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"rendered")
    stamp = time.time() + 5 * 365 * 24 * 3600
    os.utime(dest, (stamp, stamp))
    await asyncio.sleep(0.01)  # keep the two writes distinguishable in filesystem time
    _png(tmp_path / "f.png")  # the source is replaced AFTER the rendition was generated

    result = await printready.enqueue_printready(adb, [att.id])

    assert result.enqueued == (att.id,)


async def test_an_empty_rendition_file_is_not_mistaken_for_a_finished_one(
    adb, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "f.png"))
    dest = printready.printready_path(att.uid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")  # a crash between create and write

    result = await printready.enqueue_printready(adb, [att.id])

    assert result.enqueued == (att.id,)


async def test_enqueue_refuses_what_can_never_produce_a_rendition(adb, monkeypatch, tmp_path):
    # A queued job that is certain to fail would burn MEDIA_MAX_ATTEMPTS and leave a failure on
    # the record for work that was never possible.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    video = await _persisted(adb, tmp_path, mime="video/mp4", source=_png(tmp_path / "g.png"))
    gone = await _persisted(adb, tmp_path)  # storage_path points at nothing
    image = await _persisted(adb, tmp_path, source=_png(tmp_path / "h.png"))

    result = await printready.enqueue_printready(adb, [video.id, gone.id, 10**9, image.id])

    assert result.ineligible == (video.id, gone.id, 10**9)
    assert result.enqueued == (image.id,)
    assert await _jobs_for(adb, video.id) == [] and await _jobs_for(adb, gone.id) == []


async def test_enqueue_prefers_the_print_master_as_the_freshness_source(
    adb, monkeypatch, tmp_path
):
    # The rendition derives from the print master, so it is the master's age that decides whether
    # the file on disk is current — an original touched later is irrelevant.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    master = _png(tmp_path / "master.png")
    att = await _persisted(
        adb,
        tmp_path,
        source=tmp_path / "orig.png",
        renditions=[
            AttachmentRendition(
                variant_key="print:glossy",
                label="glossy",
                filename="master.png",
                mime="image/png",
                size=1,
                storage_path=str(master),
            )
        ],
    )
    dest = printready.printready_path(att.uid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"rendered")
    await asyncio.sleep(0.01)  # keep the two writes distinguishable in filesystem time
    _png(tmp_path / "orig.png")  # the ORIGINAL is rewritten after the rendition — not the source

    assert printready.source_path(att) == master
    result = await printready.enqueue_printready(adb, [att.id])
    assert result.fresh == (att.id,)


async def test_enqueue_of_nothing_touches_nothing(adb):
    assert await printready.enqueue_printready(adb, []) == printready.EnqueueResult((), (), (), ())


# --- the worker end ------------------------------------------------------------------------


def _seed_job(mime: str, storage_path: str, kind: str, lane: str = "slow") -> tuple[int, int, str]:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO attachments (uid, filename, mime, size, status, storage_path) "
                "VALUES (:u,'x',:m,1,CAST('ready' AS attachment_status),:sp) RETURNING id"
            ),
            {"u": secrets.token_urlsafe(8), "m": mime, "sp": storage_path},
        ).scalar_one()
        uid = c.execute(text("SELECT uid FROM attachments WHERE id=:a"), {"a": aid}).scalar_one()
        jid = c.execute(
            text(
                "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct) "
                "VALUES (:a, CAST(:l AS job_lane), :k, CAST('queued' AS job_state), 0, 0) "
                "RETURNING id"
            ),
            {"a": aid, "l": lane, "k": kind},
        ).scalar_one()
    eng.dispose()
    return aid, jid, uid


def _read_job(job_id: int) -> tuple[str, int]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT state, attempts FROM media_jobs WHERE id=:j"), {"j": job_id}
        ).one()
    eng.dispose()
    return row[0], row[1]


def _read_status(attachment_id: int) -> str:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        status = c.execute(
            text("SELECT status FROM attachments WHERE id=:a"), {"a": attachment_id}
        ).scalar_one()
    eng.dispose()
    return status


def test_the_worker_generates_the_tier_without_touching_the_photo(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _png(tmp_path / "worker.png", size=(4200, 2100))
    aid, jid, uid = _seed_job("image/png", str(src), JobKind.printready)
    try:

        async def _go() -> str:
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"

        out = _decode(printready.printready_path(uid))
        assert out.size == (4096, 2048) and out.info.get("dpi") == (300, 300)
        assert _read_job(jid)[0] == "done"
        # It publishes an EXTRA tier: the photo's own row is left exactly as it was.
        assert _read_status(aid) == "ready"
    finally:
        _delete_attachments(aid)


def test_a_failing_print_job_never_marks_the_photo_failed(monkeypatch, tmp_path):
    # Isolation: the screen tiers of this photo are published and fine. Blanking it out of the
    # feed because an on-demand print rendition could not be encoded would be a self-inflicted
    # outage — the failure belongs on the job row (where enqueue reads it back).
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)
    aid, jid, _ = _seed_job("image/png", str(tmp_path / "does-not-exist.png"), JobKind.printready)
    try:

        async def _go() -> str:
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"  # terminal failure recorded, worker survives

        assert _read_job(jid) == ("failed", 1)
        assert _read_status(aid) == "ready"  # the photo is still a photo
    finally:
        _delete_attachments(aid)


def test_only_the_upload_pipeline_notifies_the_owner(monkeypatch, tmp_path):
    # A book asks for a few hundred renditions at once; if each one pushed "your photo is ready"
    # the owner's phone would buzz for every photo they already have.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    sent: list[int | None] = []

    async def _spy(db, user_id, payload) -> int:
        sent.append(user_id)
        return 1

    monkeypatch.setattr(worker.push, "send_to_user", _spy)
    src = _png(tmp_path / "notify.png", size=(60, 40))
    print_aid, _, _ = _seed_job("image/png", str(src), JobKind.printready)
    derive_aid, _, _ = _seed_job("image/png", str(src), JobKind.derive, lane="fast")
    try:

        async def _drain() -> None:
            for _ in range(4):
                if await worker.run_media_drain() == "idle":
                    return

        asyncio.run(_drain())

        assert len(sent) == 1  # exactly the derive job — the print job stayed silent
    finally:
        _delete_attachments(print_aid, derive_aid)


async def test_a_print_job_defers_while_the_host_is_too_hot(adb, monkeypatch, tmp_path):
    # Same guard as the transcode, for the same reason: this runs on a NAS shared with production
    # whose real risk is a thermal shutdown, not a slow queue.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "too_hot", lambda: True)
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "hot.png"))
    job = MediaJob(
        attachment_id=att.id, lane=JobLane.slow, kind=JobKind.printready, state=JobState.queued
    )
    adb.add(job)
    await adb.flush()

    with pytest.raises(media.ThermalDefer):
        await media.process_job(adb, job)

    assert not printready.printready_path(att.uid).exists()


async def test_a_print_job_dispatches_by_kind_not_by_lane(adb, monkeypatch, tmp_path):
    # The slow lane also carries heavy-image DERIVE work (HEIC), which re-runs the photo's own
    # tiers and rewrites width/height/blurhash. A printready job must never fall into that branch.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "kind.png", size=(120, 90)))
    att.blurhash = None
    job = MediaJob(
        attachment_id=att.id, lane=JobLane.slow, kind=JobKind.printready, state=JobState.queued
    )
    adb.add(job)
    await adb.flush()

    await media.process_job(adb, job)

    assert printready.printready_path(att.uid).exists()
    assert not (tmp_path / "assets" / "thumbs" / f"{att.uid}.jpg").exists()
    assert att.blurhash is None and att.width is None  # the photo's own columns are untouched


async def test_the_upload_pipeline_never_generates_the_print_tier(adb, monkeypatch, tmp_path):
    # On-demand only: a 60 MP re-encode per upload is exactly the cost this tier is designed to
    # pay once, when a book needs it.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = await _persisted(adb, tmp_path, source=_png(tmp_path / "upload.png", size=(120, 90)))
    job = MediaJob(
        attachment_id=att.id, lane=JobLane.fast, kind=JobKind.derive, state=JobState.queued
    )
    adb.add(job)
    await adb.flush()

    await media.process_job(adb, job)

    assert (tmp_path / "assets" / "thumbs" / f"{att.uid}.jpg").exists()  # the tiers it does make
    assert not printready.printready_path(att.uid).exists()
    kinds = [j.kind for j in await _jobs_for(adb, att.id)]
    assert JobKind.printready not in kinds  # and it enqueues no print work either


# --- profile identity (MEMNG-238) -------------------------------------------------------------


def test_the_default_profile_is_the_shipped_tier():
    """Every literal here is what the tier ON DISK was built with. If any of them moves, every
    existing print rendition silently becomes a file for a profile nobody asked for."""
    profile = printready.DEFAULT_PROFILE

    assert profile.long_edge == 4096
    assert profile.jpeg_quality == 92
    assert profile.metadata_policy == "strip"
    assert profile.grayscale_single_channel is True
    assert profile.key == "print-4096"
    assert profile.subdir is None


@pytest.mark.parametrize(
    ("profile", "key"),
    [
        (printready.PrintProfile(5000, 92, "strip", True), "print-5000-q92-strip-gray"),
        (printready.PrintProfile(4096, 90, "strip", True), "print-4096-q90-strip-gray"),
        (printready.PrintProfile(4096, 92, "keep_dates", True), "print-4096-q92-dates-gray"),
        (printready.PrintProfile(4096, 92, "strip", False), "print-4096-q92-strip-rgb"),
        (printready.PrintProfile(0, 95, "keep_dates", False), "print-orig-q95-dates-rgb"),
    ],
)
def test_every_byte_affecting_field_is_in_the_key(profile, key):
    """Spelled out rather than re-derived: a test that called `.key` to predict `.key` would pass
    with any encoding at all, including one that dropped a field."""
    assert profile.key == key
    assert profile.subdir == key
    assert printready.profile_from_key(key) == profile


@pytest.mark.parametrize(
    "key",
    [
        "print-4096-q92-strip-gray",  # a second spelling of the default
        "print-04096-q92-strip-gray",  # leading zero
        "print-5000-q92-strip",  # a field missing
        "print-5000-q92-strip-gray-extra",
        "print-5000-q92-keep_dates-gray",  # the stored value, not the key token
        "print-2560",
        "printready/../../etc",
        "",
    ],
)
def test_a_key_that_is_not_canonical_parses_to_nothing(key):
    assert printready.profile_from_key(key) is None


def test_a_profile_derives_into_its_own_directory_and_the_default_stays_put(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    other = printready.PrintProfile(5000, 90, "strip", True)

    assert printready.printready_path("u1") == tmp_path / "assets" / "printready" / "u1.jpg"
    assert printready.printready_path("u1", printready.DEFAULT_PROFILE) == (
        tmp_path / "assets" / "printready" / "u1.jpg"
    )
    assert printready.printready_path("u1", other) == (
        tmp_path / "assets" / "printready" / "print-5000-q90-strip-gray" / "u1.jpg"
    )


def test_profile_variant_paths_enumerate_what_is_on_disk(monkeypatch, tmp_path):
    """Enumerated, not derived: the set of profiles that ever existed is unbounded, so a deletion
    path that listed only the configured ones would leak a file per abandoned profile."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    root = tmp_path / "assets" / "printready"
    (root / "print-5000-q90-strip-gray").mkdir(parents=True)
    (root / "print-orig-q95-dates-rgb").mkdir(parents=True)
    (root / "print-5000-q90-strip-gray" / "u1.jpg").write_bytes(b"x")
    (root / "print-orig-q95-dates-rgb" / "u1.jpg").write_bytes(b"y")
    (root / "print-orig-q95-dates-rgb" / "u2.jpg").write_bytes(b"z")
    (root / "u1.jpg").write_bytes(b"legacy")

    (root / "print-5000-q90-strip-gray" / "u1.jpg.tmp").write_bytes(b"half-written")
    (root / "print-5000-q90-strip-gray" / "u10.jpg").write_bytes(b"a different uid")

    found = printready.profile_variant_paths_sync("u1")

    assert found == [
        root / "print-5000-q90-strip-gray" / "u1.jpg",
        root / "print-5000-q90-strip-gray" / "u1.jpg.tmp",
        root / "print-orig-q95-dates-rgb" / "u1.jpg",
    ]
    assert root / "u1.jpg" not in found  # the legacy path is every caller's own
    # The trailing dot in the prefix is what keeps `u10.jpg` out of `u1`'s set.
    assert root / "print-5000-q90-strip-gray" / "u10.jpg" not in found
    assert printready.profile_variant_paths_sync("nobody") == []


def test_no_tier_directory_at_all_enumerates_nothing(monkeypatch, tmp_path):
    """The ONE absence that may be answered "nothing": the tier has never been written."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    assert printready.profile_variant_paths_sync("u1") == []


@pytest.mark.parametrize("unreadable", ["the tier root", "a profile directory"])
def test_an_unreadable_directory_raises_rather_than_reporting_nothing(
    monkeypatch, tmp_path, unreadable
):
    """The distinction this whole contract exists for: "I looked and there is nothing" versus "I
    could not look". Answering the second like the first is how a transiently unreadable mount
    would let a reclaim delete the row while the renditions survive with nothing naming them.

    BOTH listings are covered, because they are two separate `except` clauses: the tier root's and
    each profile directory's. Either one widened to bare OSError re-opens the hole on its own.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    root = tmp_path / "assets" / "printready"
    profile_dir = root / "print-5000-q90-strip-gray"
    profile_dir.mkdir(parents=True)
    (profile_dir / "u1.jpg").write_bytes(b"rendition")
    # Positive control: it IS found while both directories can be read.
    assert printready.profile_variant_paths_sync("u1") == [profile_dir / "u1.jpg"]

    blocked = root if unreadable == "the tier root" else profile_dir
    blocked.chmod(0o000)
    try:
        with pytest.raises(OSError):
            printready.profile_variant_paths_sync("u1")
    finally:
        blocked.chmod(0o755)


def _attachment_rows(uid: str) -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        count = c.execute(
            text("SELECT count(*) FROM attachments WHERE uid = :u"), {"u": uid}
        ).scalar_one()
    eng.dispose()
    return count


def test_an_unreadable_profile_directory_stops_the_reclaim_and_keeps_the_row(
    logged_in_client, monkeypatch, tmp_path
):
    """Fail closed, end to end: the delete is refused, the row survives, and the files are all
    still there — the state a later retry can finish, rather than an orphan nothing names."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    att_id = _seed_attachment("pr-scan-fail", subject)
    legacy = printready.printready_path("pr-scan-fail")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"rendition")
    profile_dir = legacy.parent / "print-5000-q90-strip-gray"
    profile_dir.mkdir(parents=True)
    (profile_dir / "pr-scan-fail.jpg").write_bytes(b"other profile")
    profile_dir.chmod(0o000)
    try:
        with capture_logs() as logs:
            response = client.delete("/api/attachments/pr-scan-fail")
        assert response.status_code == 500, response.text
        assert any(entry["event"] == "reclaim.profile_scan_failed" for entry in logs)
        # The row is intact — the reclaim rolled back rather than committing a half-done delete.
        assert _attachment_rows("pr-scan-fail") == 1
        assert legacy.exists(), "a file was removed by a reclaim that reported it had removed none"
    finally:
        profile_dir.chmod(0o755)
        _delete_attachments(att_id)


def test_an_interrupted_encodes_temp_file_is_reclaimed_too(logged_in_client, monkeypatch, tmp_path):
    """The leak B2 closes: `_atomic_save` writes `<uid>.jpg.tmp` and renames it, so a worker killed
    in between leaves a temp file in a profile directory that no derived list ever named."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    att_id = _seed_attachment("pr-tmp-leak", subject)
    profile = printready.PrintProfile(5000, 90, "strip", True)
    final = printready.printready_path("pr-tmp-leak", profile)
    orphaned_tmp = final.with_suffix(final.suffix + ".tmp")
    final.parent.mkdir(parents=True, exist_ok=True)
    orphaned_tmp.write_bytes(b"half a rendition")
    try:
        assert client.delete("/api/attachments/pr-tmp-leak").status_code == 204
    finally:
        _delete_attachments(att_id)

    assert not orphaned_tmp.exists(), "an interrupted encode's temp file outlived its attachment"


def test_a_jobs_sweep_takes_the_temp_files_of_every_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(0, 95, "strip", False)
    final = printready.printready_path("pr-sweep-tmp", profile)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = final.with_suffix(final.suffix + ".tmp")
    tmp_file.write_bytes(b"half")

    assert tmp_file in media.derived_outputs("pr-sweep-tmp")


def test_a_worker_sweep_survives_an_unreadable_profile_directory(monkeypatch, tmp_path):
    """The opposite disposition to reclaim's, deliberately: `discard_outputs`'s caller has already
    established that the row is GONE, so failing the job would reap nothing at all. The other tiers
    are still swept and the unreadable directory is named in the log."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    legacy = printready.printready_path("pr-sweep-blind")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    profile_dir = legacy.parent / "print-5000-q90-strip-gray"
    profile_dir.mkdir(parents=True)
    profile_dir.chmod(0o000)
    try:
        with capture_logs() as logs:
            outputs = media.derived_outputs("pr-sweep-blind")
    finally:
        profile_dir.chmod(0o755)

    assert any(entry["event"] == "printready_profile_scan_failed" for entry in logs)
    assert legacy in outputs  # the rest of the sweep still happened


def test_a_deleted_photo_takes_every_profiles_rendition_with_it(
    logged_in_client, monkeypatch, tmp_path
):
    """The leak this closes: a rendition in a profile directory outliving the row that named it.

    Driven through the real DELETE endpoint, and the abandoned profile is one settings no longer
    name — which is exactly the case a derived (rather than enumerated) list would miss.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    att_id = _seed_attachment("pr-prof-del", subject)
    legacy = printready.printready_path("pr-prof-del")
    abandoned = printready.printready_path(
        "pr-prof-del", printready.PrintProfile(5000, 90, "keep_dates", False)
    )
    for path in (legacy, abandoned):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"rendition")
    try:
        assert client.delete("/api/attachments/pr-prof-del").status_code == 204
    finally:
        _delete_attachments(att_id)

    assert not legacy.exists()
    assert not abandoned.exists(), "a profile directory's rendition outlived its attachment"


def test_a_jobs_sweep_finds_every_profiles_rendition_too(monkeypatch, tmp_path):
    """The worker's own cleanup (media.derived_outputs) has to enumerate the same set: a job that
    discovers its attachment was deleted mid-encode sweeps its output through this list."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    abandoned = printready.printready_path(
        "pr-sweep", printready.PrintProfile(0, 95, "strip", False)
    )
    abandoned.parent.mkdir(parents=True, exist_ok=True)
    abandoned.write_bytes(b"rendition")

    outputs = media.derived_outputs("pr-sweep")

    assert abandoned in outputs
    assert printready.printready_path("pr-sweep") in outputs  # the legacy one, still


# --- the encoder follows the profile ----------------------------------------------------------


def test_a_profile_decides_the_long_edge_the_quality_and_the_path(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(5000, 90, "strip", False)
    att = _attachment(_png(tmp_path / "big.png", size=(6000, 3000)), uid="pr-prof-enc")

    dest = media.process_printready(att, profile)

    assert dest == (
        tmp_path / "assets" / "printready" / "print-5000-q90-strip-rgb" / "pr-prof-enc.jpg"
    )
    out = _decode(dest)
    assert out.size == (5000, 2500)
    assert out.info.get("dpi") == (300, 300)  # print correctness, true of every profile
    assert JpegImagePlugin.get_sampling(out) == 0
    # ...and nothing was written at the default profile's path.
    assert not printready.printready_path("pr-prof-enc").exists()


def test_a_profile_with_no_cap_keeps_the_sources_own_size(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(printready.ORIGINAL_LONG_EDGE, 92, "strip", False)
    att = _attachment(_png(tmp_path / "orig.png", size=(6000, 3000)), uid="pr-prof-orig")

    assert _decode(media.process_printready(att, profile)).size == (6000, 3000)


def test_a_higher_quality_profile_writes_a_bigger_file(monkeypatch, tmp_path):
    """The quality really reaches the encoder, measured off the two files rather than trusted."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = _photo(tmp_path / "detail.png", _colour_pixels)
    low = media.process_printready(
        _attachment(source, uid="pr-q90"), printready.PrintProfile(4096, 90, "strip", False)
    )
    high = media.process_printready(
        _attachment(source, uid="pr-q95"), printready.PrintProfile(4096, 95, "strip", False)
    )

    assert high.stat().st_size > low.stat().st_size


# --- a source the encoder's write buffer cannot hold (MEMNG-241) ------------------------------


def _high_entropy_png(path: Path, size=(512, 512)) -> Path:
    """A source whose encoded SCAN is bigger than its pixel count — the shape MEMNG-241 is about.

    Uniform per-pixel noise is the most an 8-bit source can carry, and at 4:4:4 it encodes here to
    ~1.9 bytes per pixel: past the one-byte-per-pixel buffer Pillow gives an optimized save, which
    is exactly the condition that used to abort the encode. The large-scale warm/cool split is
    what keeps it a COLOUR photograph to the classifier — chroma is measured on a downsampled
    sample, where unbiased noise averages to flat grey, and a rendition written single-channel
    would fit the buffer after all and prove nothing. Built in code, never committed (convention).
    """
    width, height = size
    rng = random.Random(20260909)
    raw = bytearray(rng.getrandbits(8) for _ in range(width * height * 3))
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 3
            raw[i + (2 if x < width // 2 else 0)] //= 4
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", size, bytes(raw)).save(path, "PNG")
    return path


def test_a_high_entropy_source_encodes_at_the_default_profile(monkeypatch, tmp_path):
    """AC: the default rung survives a source whose scan does not fit the write buffer libjpeg is
    handed. `optimize=True` cannot suspend mid-scan, so this used to raise OSError('broken data
    stream when writing image file') and fail the job for an image that saves fine without it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_high_entropy_png(tmp_path / "entropy.png"), uid="pr-entropy-default")

    dest = media.process_printready(att)

    assert dest == printready.printready_path("pr-entropy-default")
    out = _decode(dest)
    assert out.mode == "RGB"  # a single-channel rendition would fit the buffer and prove nothing
    assert out.size == (512, 512)
    assert JpegImagePlugin.get_sampling(out) == 0
    # The file is the evidence that the encode really was the overflowing one: more bytes of scan
    # than the pixel count Pillow sizes the buffer from.
    assert dest.stat().st_size > 512 * 512


def test_a_high_entropy_source_encodes_at_a_versioned_profile(monkeypatch, tmp_path):
    """The other save path: a profile with its own directory reaches the same encoder, so the
    buffer has to be sized there too — a fix that only covered the default rung would leave every
    settings-chosen profile raising."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(5000, 90, "strip", False)
    att = _attachment(_high_entropy_png(tmp_path / "entropy.png"), uid="pr-entropy-profile")

    dest = media.process_printready(att, profile)

    assert dest == (
        tmp_path / "assets" / "printready" / "print-5000-q90-strip-rgb" / "pr-entropy-profile.jpg"
    )
    assert _decode(dest).size == (512, 512)  # under the cap: never upscaled
    assert dest.stat().st_size > 512 * 512
    assert not printready.printready_path("pr-entropy-profile").exists()


def test_the_encoder_leaves_pillows_buffer_size_where_it_found_it(monkeypatch, tmp_path):
    """The headroom is scoped to the save. `ImageFile.MAXBLOCK` is a module attribute shared with
    every other Pillow user in the process — a derive that left it raised would quietly change how
    every later encode in the app is buffered, and nothing would say why."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # Pinned to Pillow's own default rather than read off the module: whatever ran before this
    # test in the same process would otherwise BE the expected value, and a derive that never put
    # the raised buffer back would still pass.
    monkeypatch.setattr(ImageFile, "MAXBLOCK", 65536)
    att = _attachment(_high_entropy_png(tmp_path / "entropy.png"), uid="pr-entropy-scope")

    media.process_printready(att)

    assert ImageFile.MAXBLOCK == 65536


def _narrow_high_entropy_png(path: Path, size=(2, 5000)) -> Path:
    """The same noise, in a picture two pixels wide.

    A JPEG codes whole MCUs, so a 2 px wide frame is coded 16 px wide and pays for the padding:
    10 000 pixels here encode to ~72 kB at q95 4:4:4 — more than four bytes per PIXEL, and more
    than the 65 536-byte floor under which Pillow's own guess disappears. A bound taken from the
    pixel count alone passes every square fixture and still fails on this one.
    """
    width, height = size
    rng = random.Random(20260909)
    pixels = bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", size, pixels).save(path, "PNG")
    return path


def test_a_narrow_high_entropy_source_encodes_at_the_highest_profile(monkeypatch, tmp_path):
    """AC: every admissible profile, every shape. q95 at 5000 px is a profile the admin panel can
    save (app/routers/admin_export.py), and this shape is the one whose scan outruns a bound
    measured in pixels rather than in the MCU-padded area that is actually coded."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(5000, 95, "strip", False)
    att = _attachment(_narrow_high_entropy_png(tmp_path / "narrow.png"), uid="pr-entropy-narrow")

    dest = media.process_printready(att, profile)

    out = _decode(dest)
    assert out.size == (2, 5000)  # under the cap on both edges: never upscaled
    assert JpegImagePlugin.get_sampling(out) == 0
    # 10 000 pixels, and far more than four bytes for each of them: the padding is the point.
    assert dest.stat().st_size > 4 * 2 * 5000


def test_an_encode_the_first_buffer_cannot_hold_grows_it_and_retries(monkeypatch, tmp_path):
    """The rung the measured number is not enough for. Nothing about the FIRST bound is a proof —
    libjpeg documents no worst case — so the encoder doubles its buffer and re-encodes instead of
    failing the derive, and it re-encodes into a fresh file rather than onto the half-written one.

    Driven by starting the ladder a rung too low, which is the only honest way to reach it: an
    image that beats four bytes per padded pixel is not something this suite can construct.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(media, "_OPTIMIZED_JPEG_BYTES_PER_PIXEL", 1)
    att = _attachment(_high_entropy_png(tmp_path / "entropy.png"), uid="pr-entropy-retry")

    dest = media.process_printready(att)

    out = _decode(dest)
    assert out.size == (512, 512)
    # Bigger than the first rung's whole buffer, so the file cannot be the first attempt's work;
    # and it decodes, so it is not a half-written one either.
    assert dest.stat().st_size > 1 * 512 * 512
    assert not dest.with_suffix(".jpg.tmp").exists()


def test_the_first_buffer_bound_covers_what_that_shape_actually_encodes_to(tmp_path):
    """The bound is checked against a real encode, not against itself.

    Two pixels wide is coded sixteen pixels wide, so the padded area — not the pixel count — is
    what the scan is paid for. Both directions are asserted: the bound covers the bytes libjpeg
    actually produced, and the naive pixel-count bound would not have.
    """
    with Image.open(_narrow_high_entropy_png(tmp_path / "narrow.png")) as img:
        img.load()
        params = {"quality": 95, "subsampling": 0, "optimize": True}
        buffer = BytesIO()
        media._save_jpeg(img, buffer, **params)
        encoded = len(buffer.getvalue())
        bound = media._jpeg_buffer_bound(img, params, media._OPTIMIZED_JPEG_BYTES_PER_PIXEL)

    assert bound >= encoded
    assert media._OPTIMIZED_JPEG_BYTES_PER_PIXEL * img.width * img.height < encoded


# --- monochrome detection ---------------------------------------------------------------------


def _photo(path: Path, shade, size=(400, 300)) -> Path:
    """A synthetic photograph: a luminance ramp with per-pixel noise, coloured by `shade`.

    Noise is deliberate — a flat field compresses to nothing and would make the size comparison
    below meaningless, and a real photograph's chroma noise is what a neutrality threshold has to
    survive. Built here rather than committed as a binary (repo convention).
    """
    rng = random.Random(20260830)
    img = Image.new("RGB", size)
    pixels = img.load()
    assert pixels is not None
    for y in range(size[1]):
        for x in range(size[0]):
            luma = max(0, min(255, (x * 255) // size[0] + rng.randint(-10, 10)))
            pixels[x, y] = shade(luma, x, y, rng)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path


def _neutral_pixels(luma, _x, _y, _rng):
    return (luma, luma, luma)


def _sepia_pixels(luma, _x, _y, _rng):
    """A uniform warm tone over the same luminance — non-zero chroma, one hue everywhere."""
    return (
        min(255, int(luma * 1.07) + 18),
        int(luma * 0.90),
        int(luma * 0.62),
    )


def _colour_pixels(luma, _x, y, rng):
    """Two independent ramps and an inverted one: chroma everywhere, and its HUE varies across the
    frame — which is the property that tells a colour photograph from a toned one."""
    return (
        luma,
        max(0, min(255, y * 255 // 300 + rng.randint(-10, 10))),
        max(0, min(255, 255 - luma + rng.randint(-10, 10))),
    )


def _warm_cool_delta(img: Image.Image) -> float:
    """Mean red minus mean blue: how warm the whole frame is. A grey image scores ~0."""
    red, _, blue = img.convert("RGB").split()
    return ImageStat.Stat(red).mean[0] - ImageStat.Stat(blue).mean[0]


def test_the_three_way_classification(tmp_path):
    """Each input category gets its own case: a single-category test lets a classifier that answers
    one thing for everything pass."""
    with Image.open(_photo(tmp_path / "n.png", _neutral_pixels)) as neutral:
        assert media.classify_chromaticity(neutral.convert("RGB")) == "neutral"
    with Image.open(_photo(tmp_path / "s.png", _sepia_pixels)) as sepia:
        assert media.classify_chromaticity(sepia.convert("RGB")) == "toned"
    with Image.open(_photo(tmp_path / "c.png", _colour_pixels)) as colour:
        assert media.classify_chromaticity(colour.convert("RGB")) == "color"


def test_a_neutral_photo_is_encoded_single_channel_and_smaller_at_the_same_quality(
    monkeypatch, tmp_path
):
    """AC: the size win is the ABSENT CHROMA, never a quality drop.

    Both files come out of the same source at the same quality — only the toggle differs — so the
    difference is exactly the two chroma planes and the sRGB profile that a one-component JPEG
    cannot carry.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = _photo(tmp_path / "bw.png", _neutral_pixels)
    on = printready.PrintProfile(4096, 92, "strip", True)
    off = printready.PrintProfile(4096, 92, "strip", False)

    single = media.process_printready(_attachment(source, uid="pr-mono-on"), on)
    rgb = media.process_printready(_attachment(source, uid="pr-mono-off"), off)

    assert _decode(single).mode == "L"
    assert _decode(rgb).mode == "RGB"
    assert single.stat().st_size < rgb.stat().st_size
    # A single-component JPEG carries no three-component profile — see process_printready.
    assert not _decode(single).info.get("icc_profile")
    assert _decode(rgb).info.get("icc_profile")


def test_a_sepia_photo_stays_rgb_with_its_toning_intact(monkeypatch, tmp_path):
    """The verdict that has to be right: sepia is monochrome to the eye, and encoding it
    single-channel would hand back a grey image with the toning thrown away.

    The channel deltas are read out of the DECODED rendition and compared against the source's own —
    averaged over the whole frame rather than sampled at one pixel, because q92 smooths per-pixel
    noise and a single sample would be measuring the encoder. A regression that greyed the image
    collapses both deltas to ~0; one that shifted the hue moves them apart.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = _photo(tmp_path / "sepia.png", _sepia_pixels)
    att = _attachment(source, uid="pr-sepia")

    out = _decode(media.process_printready(att, printready.PrintProfile(4096, 92, "strip", True)))

    assert out.mode == "RGB"
    with Image.open(source) as raw:
        before = _warm_cool_delta(raw.convert("RGB"))
    after = _warm_cool_delta(out)
    assert before > 25, before  # positive control: the fixture really is toned
    assert after > 25, f"the toning was flattened: {after}"
    assert abs(after - before) <= 3, (before, after)


def test_a_colour_photo_stays_rgb(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_photo(tmp_path / "colour.png", _colour_pixels), uid="pr-colour")

    out = _decode(media.process_printready(att, printready.PrintProfile(4096, 92, "strip", True)))

    assert out.mode == "RGB"
    assert out.info.get("icc_profile")


@pytest.mark.parametrize("shade", [_neutral_pixels, _sepia_pixels, _colour_pixels])
def test_the_toggle_off_disables_the_whole_path(monkeypatch, tmp_path, shade):
    """AC: with the toggle off nothing is ever single-channel, the neutral photo included."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_photo(tmp_path / f"{shade.__name__}.png", shade), uid=f"pr-off{id(shade)}")

    out = _decode(media.process_printready(att, printready.PrintProfile(4096, 92, "strip", False)))

    assert out.mode == "RGB"
    assert out.info.get("icc_profile")


def test_detection_runs_on_the_managed_pixels_not_the_raw_ones(monkeypatch, tmp_path):
    """Order, asserted through behaviour: a neutral image TAGGED with a wide-gamut profile is still
    neutral after the transform, and would only look otherwise if the statistics were taken before
    the colour management or the 16-bit/alpha normalisation."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = _photo(tmp_path / "wide-bw.png", _neutral_pixels)
    with Image.open(source) as raw:
        raw.convert("RGB").save(source, "PNG", icc_profile=wide_gamut_icc())
    att = _attachment(source, uid="pr-wide-bw")

    assert _decode(media.process_printready(att)).mode == "L"


def test_a_16_bit_source_is_normalised_rather_than_clipped(monkeypatch, tmp_path):
    """Pillow's `convert("RGB")` reads an `I;16` sample as 8-bit and CLIPS: everything above 255
    comes back white, so a 16-bit scan would print as a blown-out field. Measured on the rendition:
    a full-range 16-bit ramp has to come back as a ramp, not as a flat 255."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = tmp_path / "deep.png"
    ramp = Image.new("I;16", (256, 8))
    ramp.putdata([column * 257 for _row in range(8) for column in range(256)])
    ramp.save(source, "PNG")
    att = _attachment(source, uid="pr-16bit")

    out = _decode(media.process_printready(att))

    dark, mid, light = out.getpixel((4, 4)), out.getpixel((128, 4)), out.getpixel((250, 4))
    assert isinstance(dark, int) and isinstance(mid, int) and isinstance(light, int)
    assert dark < 20 and 100 < mid < 160 and light > 230, (dark, mid, light)


@pytest.mark.parametrize("mode", ["I;16", "I;16B"])
def test_a_16_bit_source_of_either_endianness_produces_a_rendition(monkeypatch, tmp_path, mode):
    """`point` is implemented for the NATIVE `I;16` only: on the `I;16B` a big-endian TIFF decodes
    to it raises ValueError, which is not a bad file — it is a good file the worker would retry to
    exhaustion. Both spellings carry the same picture, so both must come back as the same ramp.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = tmp_path / f"deep-{mode.replace(';', '')}.tiff"
    packer = ">H" if mode.endswith("B") else "<H"
    raw = b"".join(struct.pack(packer, column * 257) for _row in range(8) for column in range(256))
    Image.frombytes(mode, (256, 8), raw).save(source, "TIFF")
    with Image.open(source) as reopened:
        assert reopened.mode == mode, f"the fixture did not survive as {mode}"
    att = _attachment(source, uid=f"pr-16-{mode.replace(';', '')}")

    out = _decode(media.process_printready(att))

    dark, mid, light = out.getpixel((4, 4)), out.getpixel((128, 4)), out.getpixel((250, 4))
    assert isinstance(dark, int) and isinstance(mid, int) and isinstance(light, int)
    assert dark < 20 and 100 < mid < 160 and light > 230, (mode, dark, mid, light)


def test_a_transparent_source_is_composited_onto_white(monkeypatch, tmp_path):
    """A transparent PNG can carry any colour in its invisible pixels; `convert("RGB")` keeps that
    colour and prints it. Paper is white, so that is what they are composited onto — and detection
    then measures the pixels that will actually be printed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    source = tmp_path / "alpha.png"
    rgba = Image.new("RGBA", (40, 30), (255, 0, 0, 0))  # fully transparent red
    rgba.save(source, "PNG")
    att = _attachment(source, uid="pr-alpha")

    out = _decode(media.process_printready(att))
    pixel = out.getpixel((10, 10))

    assert pixel == 255 if out.mode == "L" else pixel == (255, 255, 255)


# --- metadata policy --------------------------------------------------------------------------


def _source_with_metadata(path: Path) -> Path:
    """A JPEG carrying everything a camera writes and a print service must not receive."""
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation — already baked into the pixels by the time it is encoded
    exif[0x010F] = "ACME"  # Make
    exif[0x0110] = "Model X"  # Model
    exif.get_ifd(0x8769).update({0x9003: "2019:07:04 12:30:00", 0x9004: "2019:07:04 12:31:00"})
    exif.get_ifd(0x8825).update(
        {
            1: "N",
            2: (IFDRational(47, 1), IFDRational(22, 1), IFDRational(0, 1)),
            3: "E",
            4: (IFDRational(8, 1), IFDRational(32, 1), IFDRational(0, 1)),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), (200, 90, 40)).save(path, "JPEG", exif=exif.tobytes())
    return path


def test_the_default_policy_writes_no_metadata_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    att = _attachment(_source_with_metadata(tmp_path / "strip.jpg"), uid="pr-strip")

    out = _decode(media.process_printready(att))

    assert dict(out.getexif()) == {}


def test_keep_dates_rebuilds_the_dates_and_leaves_the_gps_behind(monkeypatch, tmp_path):
    """A whitelist that is CONSTRUCTED cannot leak a tag nobody thought to remove.

    The source carries GPS coordinates, the camera's make and model and an Orientation that is
    already baked into the pixels; the rendition is about to be uploaded to a print service. The
    dates survive because they were named; nothing else survives because nothing else was.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(4096, 92, "keep_dates", True)
    source = _source_with_metadata(tmp_path / "dates.jpg")
    att = _attachment(source, uid="pr-dates")

    out = _decode(media.process_printready(att, profile))

    exif = out.getexif()
    assert exif.get_ifd(0x8769) == {
        0x9003: "2019:07:04 12:30:00",
        0x9004: "2019:07:04 12:31:00",
    }
    assert exif.get_ifd(0x8825) == {}, "GPS coordinates survived into a print rendition"
    assert 0x010F not in exif and 0x0110 not in exif  # camera make/model
    assert 0x0112 not in exif  # a stale Orientation would re-rotate an already-baked image
    # Positive control: the source really did carry all of it.
    with Image.open(source) as raw:
        assert raw.getexif().get_ifd(0x8825)
        assert raw.getexif()[0x0112] == 6


def test_keep_dates_on_a_source_with_no_dates_writes_no_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    profile = printready.PrintProfile(4096, 92, "keep_dates", True)
    att = _attachment(_png(tmp_path / "bare.png"), uid="pr-nodates")

    assert dict(_decode(media.process_printready(att, profile)).getexif()) == {}


# --- the worker encodes what the job asked for ------------------------------------------------


def _set_job_key(job_id: int, key: str) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text("UPDATE media_jobs SET profile_key = :k WHERE id = :j"), {"k": key, "j": job_id}
        )
    eng.dispose()


def test_the_worker_encodes_the_profile_the_job_names_not_the_current_settings(
    monkeypatch, tmp_path
):
    """The snapshot is the point: minutes or hours separate the request from the encode, and the
    settings may have moved in between. The bytes have to be the ones that were asked for, at the
    path the request already told the client about."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _png(tmp_path / "worker-profile.png", size=(6000, 3000))
    aid, jid, uid = _seed_job("image/png", str(src), JobKind.printready)
    _set_job_key(jid, "print-5000-q90-strip-rgb")
    try:

        async def _go() -> str:
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"
        assert _read_job(jid)[0] == "done"
    finally:
        _delete_attachments(aid)

    derived = tmp_path / "assets" / "printready" / "print-5000-q90-strip-rgb" / f"{uid}.jpg"
    assert derived.exists()
    assert _decode(derived).size == (5000, 2500)
    # The default tier was never written: a book at one profile must not publish another's file.
    assert not printready.printready_path(uid).exists()


def test_a_job_with_no_profile_key_still_writes_the_shipped_tier(monkeypatch, tmp_path):
    """The rolling-deploy case: a job row enqueued by the old code, drained by the new worker."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _png(tmp_path / "legacy-job.png", size=(4200, 2100))
    aid, jid, uid = _seed_job("image/png", str(src), JobKind.printready)  # writes no profile_key
    try:

        async def _go() -> str:
            async with async_session() as db:
                return await worker.run_once(db)

        assert asyncio.run(_go()) == "processed"
    finally:
        _delete_attachments(aid)

    assert _decode(printready.printready_path(uid)).size == (4096, 2048)


def test_a_job_naming_a_profile_that_is_not_one_fails_loudly(monkeypatch, tmp_path):
    """Only a hand-made row can get here — enqueue writes a canonical key. Guessing a profile would
    publish bytes nobody asked for at a path nobody will look at."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)
    src = _png(tmp_path / "bad-job.png")
    aid, jid, uid = _seed_job("image/png", str(src), JobKind.printready)
    _set_job_key(jid, "print-9999-nonsense")
    try:

        async def _go() -> str:
            async with async_session() as db:
                return await worker.run_once(db)

        asyncio.run(_go())
        assert _read_job(jid)[0] == "failed"
    finally:
        _delete_attachments(aid)

    assert not printready.printready_path(uid).exists()


def test_the_deletion_sweep_enumerates_profile_directories_off_the_loop(
    logged_in_client, monkeypatch, tmp_path
):
    """A directory listing on this deployment's data volume is a network round-trip, and this one
    happens on the REQUEST loop — so it has to be off it, or every in-flight request stalls behind
    a delete (MEMNG-42).

    Asserted from INSIDE the call: the enumeration asks whether an event loop is running on the
    thread it finds itself on. On the loop, `get_running_loop` returns one and the call is
    blocking it; in a worker thread it raises, which is the whole property. Comparing thread ids
    with the test's own would prove nothing — Starlette's TestClient already runs the app on a
    thread of its own, so they differ either way.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    att_id = _seed_attachment("pr-offloop", subject)
    on_the_loop: list[bool] = []
    real = printready.profile_variant_paths_sync

    def recording(uid: str):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_the_loop.append(False)
        else:
            on_the_loop.append(True)
        return real(uid)

    monkeypatch.setattr(printready, "profile_variant_paths_sync", recording)
    try:
        assert client.delete("/api/attachments/pr-offloop").status_code == 204
    finally:
        _delete_attachments(att_id)

    assert on_the_loop, "the deletion path never enumerated the profile directories at all"
    assert not any(on_the_loop), "the enumeration blocked the request loop"
