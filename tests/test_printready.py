"""The print-ready rendition tier (MEMNG-49 Phase A): encode profile, source choice, enqueue, job.

The encode profile is the load-bearing part of this feature — a 4096 px JPEG that is not sRGB, or
carries no DPI, or was chroma-subsampled, still LOOKS fine on screen and prints wrong. So every
assertion here decodes the file that was actually written and reads the property back out of it;
none of them re-derives an expected value with the code under test.
"""

import asyncio
import os
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
from PIL import Image, ImageCms, JpegImagePlugin
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
