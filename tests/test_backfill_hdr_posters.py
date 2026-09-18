"""Tests for the HDR poster backfill.

Three layers, the same split the image-metadata backfill's tests use:

* the pure decisions (`exit_code`, the backup/marker paths, `ensure_backup_dir`'s fail-closed
  refusal) — asserted directly;
* the selection and the classification (`load_candidates`, dry-run counting) against the real
  schema through `adb`, with `media._ffprobe` — a subprocess call, i.e. a dependency — stubbed so
  every input category can be covered without a clip per case;
* the replacement itself, end to end against REAL ffmpeg on a synthesised HLG clip: the backup, the
  extraction, the guarded publish, the mtime restamp, the variant sweep, the blurhash row and the
  completion marker, all read back from disk and from the database rather than from the stats.

Two session styles, for a reason worth stating: the classification tests use the usual
transaction-per-test `adb`, but every test that reaches the PUBLISH sequence uses the `published`
fixture, whose rows are really committed and deleted again afterwards. The script rolls its session
back when an item fails — that is how it releases the publish guard's row lock — and a rollback
discards everything an uncommitted fixture is holding, its own seeds included. Committed rows are
also the situation the script meets in production: rows somebody else wrote long ago.

The one thing stubbed inside the replacement path is `media.attachment_still_exists` — the
worker's own row-lock primitive, in another module — which is how the "deleted while the extraction
ran" race is driven without a second connection. `still_publishable` itself is tested against real
rows.
"""

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from app.config import settings
from app.database import async_session
from app.models import Attachment, AttachmentStatus
from app.scripts import backfill_hdr_posters as bf
from app.services import blurhash_pure, media, poster
from PIL import Image
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from structlog.testing import capture_logs

OLD_BLURHASH = "LEHV6nWBaaaaaaaaaaaa"
CAPTURED_AT = datetime(2026, 8, 28, 12, 30, 15, tzinfo=UTC)
HLG = {
    "width": 320,
    "height": 240,
    "duration": 2.0,
    "color_transfer": "arib-std-b67",
    "color_primaries": "bt2020",
    "color_space": "bt2020nc",
    "pix_fmt": "yuv420p10le",
}
SDR = {
    "width": 320,
    "height": 240,
    "duration": 2.0,
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "color_space": "bt709",
    "pix_fmt": "yuv420p",
}


def _write_hlg_clip(path: Path, size="320x240", duration=2) -> Path:
    """A 10-bit clip TAGGED as HLG/BT.2020, the way a phone writes one.

    `setparams` rather than the encoder's `-color_trc`: on ffmpeg 7.1 the encoder options are
    overwritten by the filtergraph's frame properties and the file comes out with no transfer tag
    at all — an SDR clip wearing an HDR name, which would make every assertion below vacuous.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={duration}:size={size}:rate=5",
            "-vf",
            "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p10le",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return path


def _write_poster(uid: str, colour=(10, 120, 200)) -> Path:
    path = poster.poster_path(uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 240), colour).save(path, "JPEG")
    os.utime(path, (1_000_000_000, 1_000_000_000))  # any mtime but the capture date
    return path


def _write_variant(uid: str, payload: bytes = b"stale-webp") -> Path:
    path = poster.poster_path(uid).with_suffix(".webp")
    path.write_bytes(payload)
    return path


async def _seed(adb, uid: str, storage_path: str | Path | None, **kwargs) -> Attachment:
    att = Attachment(
        uid=uid,
        filename=f"{uid}.mov",
        mime=kwargs.pop("mime", "video/quicktime"),
        size=1,
        status=kwargs.pop("status", AttachmentStatus.ready),
        storage_path=str(storage_path) if storage_path is not None else None,
        captured_at=kwargs.pop("captured_at", CAPTURED_AT),
        blurhash=kwargs.pop("blurhash", OLD_BLURHASH),
        **kwargs,
    )
    adb.add(att)
    await adb.flush()
    return att


class Published:
    """A session over rows that are really in the database, plus their cleanup.

    Anything the script PUBLISHES has to be tested against committed rows: its per-item failure
    path rolls the session back, which would take an uncommitted fixture's seeds with it and make
    the next item look like a deleted attachment. Every uid seeded here is deleted when the test
    ends, whether it passed or not.
    """

    def __init__(self, session):
        self.db = session
        self.uids: list[str] = []

    async def seed(self, uid: str, storage_path, **kwargs) -> None:
        att = Attachment(
            uid=uid,
            filename=f"{uid}.mov",
            mime=kwargs.pop("mime", "video/quicktime"),
            size=1,
            status=kwargs.pop("status", AttachmentStatus.ready),
            storage_path=str(storage_path),
            captured_at=kwargs.pop("captured_at", CAPTURED_AT),
            blurhash=kwargs.pop("blurhash", OLD_BLURHASH),
            **kwargs,
        )
        self.db.add(att)
        self.uids.append(uid)
        await self.db.commit()

    async def repoint(self, uid: str, storage_path) -> None:
        await self.db.execute(
            update(Attachment).where(Attachment.uid == uid).values(storage_path=str(storage_path))
        )
        await self.db.commit()

    async def blurhash_of(self, uid: str) -> str | None:
        return (
            await self.db.execute(select(Attachment.blurhash).where(Attachment.uid == uid))
        ).scalar_one()


@pytest_asyncio.fixture
async def published(db_engine):
    async with async_session() as session:
        state = Published(session)
        try:
            yield state
        finally:
            await session.rollback()
            await session.execute(sa_delete(Attachment).where(Attachment.uid.in_(state.uids)))
            await session.commit()


def _stub_probe(monkeypatch, answers: dict[str, dict]) -> None:
    """Stand in for the ffprobe subprocess, per storage_path. An unknown path raises, which is
    what an original that is not on disk does."""

    def fake(path: str) -> dict:
        if path not in answers:
            raise FileNotFoundError(path)
        return dict(answers[path])

    monkeypatch.setattr(media, "_ffprobe", fake)


# --- the pure decisions -------------------------------------------------------------------------


def test_exit_code_is_zero_only_when_nothing_was_left_behind():
    assert bf.exit_code(bf.HdrBackfillStats(scanned=117, hdr=3, replaced=3)) == 0
    assert bf.exit_code(bf.HdrBackfillStats(scanned=117, hdr=3, replaced=2, failed=1)) == 1
    assert bf.exit_code(bf.HdrBackfillStats(scanned=117, hdr=3, replaced=2, vanished=1)) == 1
    # An HDR video skipped on purpose is not a failure — a rerun of a finished batch exits 0.
    assert bf.exit_code(bf.HdrBackfillStats(hdr=3, already_done=3)) == 0
    assert bf.exit_code(bf.HdrBackfillStats(hdr=1, missing_poster=1, skipped_sdr=9)) == 0


def test_the_backup_lives_outside_the_served_assets_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    assert bf.backup_path("abc") == tmp_path / "backfill" / "hdr-posters" / "abc.poster.jpg"
    assert bf.done_path("abc") == tmp_path / "backfill" / "hdr-posters" / "abc.done"
    # Anything under assets/ is enumerated as a derived tier; a stray JPEG there reads as an orphan.
    assert (tmp_path / "assets") not in bf.backup_path("abc").parents


def test_ensure_backup_dir_refuses_a_path_it_cannot_create(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    (tmp_path / "backfill").write_text("not a directory")

    with pytest.raises(bf.BackupRefused):
        bf.ensure_backup_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this asserts on")
def test_ensure_backup_dir_refuses_a_directory_it_cannot_write(monkeypatch, tmp_path):
    # mkdir(exist_ok=True) no-ops on an existing directory and says nothing about permission —
    # the deployed backup path is a bind-mounted share whose ACL masks the unix mode, so only a
    # real write answers. Without the probe this run would fail after overwriting a poster.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    directory = bf.backup_dir()
    directory.mkdir(parents=True)
    directory.chmod(0o555)
    try:
        with pytest.raises(bf.BackupRefused):
            bf.ensure_backup_dir()
    finally:
        directory.chmod(0o755)


# --- selection and classification ---------------------------------------------------------------


async def test_dry_run_reports_every_video_and_writes_nothing(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    hdr_src, sdr_src = tmp_path / "hdr.mov", tmp_path / "sdr.mov"
    hdr_src.write_bytes(b"x")
    sdr_src.write_bytes(b"x")
    _stub_probe(monkeypatch, {str(hdr_src): HLG, str(sdr_src): SDR})
    hdr = await _seed(adb, "dry-hdr", hdr_src)
    await _seed(adb, "dry-sdr", sdr_src)
    await _seed(adb, "dry-img", sdr_src, mime="image/jpeg")
    await _seed(adb, "dry-busy", sdr_src, status=AttachmentStatus.processing)
    await _seed(adb, "dry-nofile", None)
    before = _write_poster("dry-hdr").read_bytes()
    _write_poster("dry-sdr")
    variant = _write_variant("dry-hdr")

    with capture_logs() as logs:
        stats = await bf.run_backfill(adb, execute=False)

    assert (stats.scanned, stats.hdr, stats.skipped_sdr) == (2, 1, 1)
    assert (stats.replaced, stats.already_done, stats.missing_poster, stats.failed) == (0, 0, 0, 0)
    assert poster.poster_path("dry-hdr").read_bytes() == before  # untouched
    assert variant.exists()  # the dry run removes nothing either
    assert not bf.backup_dir().exists()  # and creates nothing
    await adb.refresh(hdr)
    assert hdr.blurhash == OLD_BLURHASH
    assert "DRY RUN" in stats.format(execute=False)

    planned = [entry for entry in logs if entry.get("uid") == "dry-hdr"]
    assert planned and planned[0]["action"] == "replace"
    assert planned[0]["color_transfer"] == "arib-std-b67"
    assert planned[0]["variants"] == ["dry-hdr.poster.webp"]  # the dry run names them


async def test_an_hdr_video_without_a_poster_is_left_to_reconcile(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = tmp_path / "hdr.mov"
    src.write_bytes(b"x")
    _stub_probe(monkeypatch, {str(src): HLG})
    await _seed(adb, "no-poster", src)

    stats = await bf.run_backfill(adb, execute=True)

    assert (stats.hdr, stats.missing_poster, stats.replaced, stats.failed) == (1, 1, 0, 0)
    assert not poster.poster_path("no-poster").exists()  # nothing derived here
    assert not bf.backup_path("no-poster").exists()
    assert not bf.done_path("no-poster").exists()
    assert bf.exit_code(stats) == 0  # a skip on purpose is not a failure


async def test_limit_caps_the_videos_looked_at(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    sources = {}
    for i in range(3):
        src = tmp_path / f"clip{i}.mov"
        src.write_bytes(b"x")
        sources[str(src)] = HLG
        await _seed(adb, f"lim{i}", src)
        _write_poster(f"lim{i}")
    _stub_probe(monkeypatch, sources)

    stats = await bf.run_backfill(adb, execute=False, limit=1)

    assert (stats.scanned, stats.hdr) == (1, 1)  # oldest first, then it stops


async def test_uid_restricts_the_run_to_one_attachment(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    hdr_src, sdr_src = tmp_path / "hdr.mov", tmp_path / "sdr.mov"
    hdr_src.write_bytes(b"x")
    sdr_src.write_bytes(b"x")
    _stub_probe(monkeypatch, {str(hdr_src): HLG, str(sdr_src): SDR})
    await _seed(adb, "victim", hdr_src)
    await _seed(adb, "bystander", sdr_src)
    _write_poster("victim")
    _write_poster("bystander")

    stats = await bf.run_backfill(adb, execute=False, uid="victim")

    assert (stats.scanned, stats.hdr, stats.skipped_sdr) == (1, 1, 0)


async def test_execute_refuses_the_whole_run_before_touching_anything(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = tmp_path / "hdr.mov"
    src.write_bytes(b"x")
    _stub_probe(monkeypatch, {str(src): HLG})
    await _seed(adb, "refuse", src)
    before = _write_poster("refuse").read_bytes()
    (tmp_path / "backfill").write_text("not a directory")  # the backups cannot be kept

    with pytest.raises(bf.BackupRefused):
        await bf.run_backfill(adb, execute=True)

    assert poster.poster_path("refuse").read_bytes() == before


# --- the publish guard --------------------------------------------------------------------------


async def test_still_publishable_answers_the_live_row(adb, tmp_path):
    att = await _seed(adb, "guarded", tmp_path / "x.mov")

    assert await bf.still_publishable(adb, att.id) is True

    att.status = AttachmentStatus.processing  # being reprocessed: not a row to publish into
    await adb.flush()
    assert await bf.still_publishable(adb, att.id) is False

    await adb.delete(att)
    await adb.flush()
    assert await bf.still_publishable(adb, att.id) is False


# --- the replacement, end to end ----------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_execute_backs_up_replaces_restamps_and_updates_the_blurhash(
    published, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(src))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    await published.seed("hdrbf-victim", src)
    old_poster = _write_poster("hdrbf-victim").read_bytes()
    old_variant = _write_variant("hdrbf-victim").read_bytes()

    stats = await bf.run_backfill(published.db, execute=True, uid="hdrbf-victim")

    assert (stats.hdr, stats.replaced, stats.failed, stats.vanished) == (1, 1, 0, 0)
    assert bf.exit_code(stats) == 0
    # 1. the original is kept, byte for byte, outside the served tree — that is the rollback
    assert bf.backup_path("hdrbf-victim").read_bytes() == old_poster
    assert (bf.backup_dir() / "hdrbf-victim.poster.webp").read_bytes() == old_variant
    # 2. the poster is a new picture at the clip's own dimensions
    new_poster = poster.poster_path("hdrbf-victim")
    assert new_poster.read_bytes() != old_poster
    with Image.open(new_poster) as image:
        assert image.size == (320, 240)
    # 3. the stale webp is gone, so a webp-capable client cannot be served the old picture
    assert not new_poster.with_suffix(".webp").exists()
    # 4. restamped: a correct replacement carries the capture date, exactly as the worker leaves
    #    it — the same mtime it had, not a new one
    assert new_poster.stat().st_mtime == CAPTURED_AT.timestamp()
    # 5. the placeholder describes the picture it fades into
    with Image.open(new_poster) as image:
        expected = blurhash_pure.encode(image)
    stored = await published.blurhash_of("hdrbf-victim")
    assert stored == expected != OLD_BLURHASH
    # 6. and the marker says what landed — written only after that commit
    marker = bf.done_path("hdrbf-victim").read_text()
    assert f"sha256={bf._sha256(new_poster)}" in marker
    assert "replaced_at=20" in marker and marker.rstrip().endswith("Z")
    assert not poster.poster_tmp_path("hdrbf-victim").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_a_failed_commit_leaves_no_marker_and_keeps_the_backup(
    published, monkeypatch, tmp_path
):
    """The marker may only ever mean "the blurhash commit went through".

    This is the ordering pinned for real: the commit is made to fail, and the assertion that no
    marker exists afterwards goes red the moment `mark_done` moves in front of `db.commit()`.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(src))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    await published.seed("hdrbf-nocommit", src)
    old_poster = _write_poster("hdrbf-nocommit").read_bytes()

    async def refused() -> None:
        raise RuntimeError("commit refused by the test")

    monkeypatch.setattr(published.db, "commit", refused)
    stats = await bf.run_backfill(published.db, execute=True, uid="hdrbf-nocommit")

    assert (stats.hdr, stats.replaced, stats.failed) == (1, 0, 1)
    assert bf.exit_code(stats) != 0
    assert not bf.done_path("hdrbf-nocommit").exists()
    assert bf.backup_path("hdrbf-nocommit").read_bytes() == old_poster


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_a_marker_that_does_not_parse_is_not_completion(published, monkeypatch, tmp_path):
    """An empty file at the marker's name — a crash mid-write, a full disk — is retried, not
    believed; and the run leaves no temporary names behind in the backup directory."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(src))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    await published.seed("hdrbf-truncated", src)
    _write_poster("hdrbf-truncated")
    bf.ensure_backup_dir()
    bf.done_path("hdrbf-truncated").write_text("")

    stats = await bf.run_backfill(published.db, execute=True, uid="hdrbf-truncated")

    assert (stats.already_done, stats.replaced, stats.failed) == (0, 1, 0)
    assert bf.is_done("hdrbf-truncated")
    assert not list(bf.backup_dir().glob("*.tmp"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_a_second_run_skips_what_is_marked_done_and_keeps_the_first_backup(
    published, monkeypatch, tmp_path
):
    # The bare --execute that follows the one-victim --uid run must not do the victim again.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(src))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    await published.seed("hdrbf-twice", src)
    original = _write_poster("hdrbf-twice").read_bytes()

    first = await bf.run_backfill(published.db, execute=True, uid="hdrbf-twice")
    replaced = poster.poster_path("hdrbf-twice").read_bytes()
    second = await bf.run_backfill(published.db, execute=True, uid="hdrbf-twice")

    assert (first.replaced, first.already_done) == (1, 0)
    assert (second.replaced, second.already_done, second.hdr) == (0, 1, 1)
    assert bf.exit_code(second) == 0
    assert poster.poster_path("hdrbf-twice").read_bytes() == replaced  # not re-derived
    assert bf.backup_path("hdrbf-twice").read_bytes() == original  # still the ORIGINAL


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_a_failed_extraction_leaves_no_marker_and_does_not_stop_the_batch(
    published, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    broken = tmp_path / "originals" / "broken.mov"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not a video")  # ffmpeg exits non-zero on it
    good = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(good))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    _stub_probe(monkeypatch, {str(broken): HLG, str(good): HLG})  # both classify as HDR
    await published.seed("hdrbf-a-broken", broken)
    await published.seed("hdrbf-b-good", good)
    kept = _write_poster("hdrbf-a-broken", colour=(200, 30, 30)).read_bytes()
    _write_poster("hdrbf-b-good")

    stats = await bf.run_backfill(published.db, execute=True)

    assert (stats.hdr, stats.failed, stats.replaced) == (2, 1, 1)  # the batch continued
    assert bf.exit_code(stats) == 1
    assert poster.poster_path("hdrbf-a-broken").read_bytes() == kept  # nothing was published
    assert not bf.done_path("hdrbf-a-broken").exists()  # so it is NOT marked done
    assert bf.backup_path("hdrbf-a-broken").read_bytes() == kept  # the backup was already taken
    assert not poster.poster_tmp_path("hdrbf-a-broken").exists()  # and the tmp swept
    assert bf.done_path("hdrbf-b-good").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_a_retry_finishes_the_uid_and_keeps_the_backup_from_the_failed_run(
    published, monkeypatch, tmp_path
):
    # The state the marker exists for: a backup with NO marker is a half-finished attempt, so the
    # retry redoes the work — and must not overwrite the original it kept the first time.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    broken = tmp_path / "originals" / "broken.mov"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not a video")
    good = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(good))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    _stub_probe(monkeypatch, {str(broken): HLG, str(good): HLG})
    await published.seed("hdrbf-retry", broken)
    original = _write_poster("hdrbf-retry").read_bytes()

    first = await bf.run_backfill(published.db, execute=True, uid="hdrbf-retry")

    assert (first.failed, first.replaced) == (1, 0)
    assert bf.backup_path("hdrbf-retry").read_bytes() == original
    assert not bf.done_path("hdrbf-retry").exists()

    await published.repoint("hdrbf-retry", good)  # the original was where the failure was
    second = await bf.run_backfill(published.db, execute=True, uid="hdrbf-retry")

    assert (second.replaced, second.already_done, second.failed) == (1, 0, 0)
    assert poster.poster_path("hdrbf-retry").read_bytes() != original
    assert bf.backup_path("hdrbf-retry").read_bytes() == original  # from the FIRST run
    assert bf.done_path("hdrbf-retry").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
async def test_an_attachment_that_goes_away_mid_flight_is_never_republished(
    published, monkeypatch, tmp_path
):
    # The delete wins the race: `media.attachment_still_exists` is the worker's own row-lock
    # primitive, and answering False is exactly what it does for a row a concurrent
    # delete_attachment has already removed. Nothing may be published into that.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    src = _write_hlg_clip(tmp_path / "originals" / "hlg.mp4")
    if media._ffprobe(str(src))["color_transfer"] != "arib-std-b67":
        pytest.skip("this ffmpeg cannot stamp HLG colour tags (setparams)")
    await published.seed("hdrbf-gone", src)
    kept = _write_poster("hdrbf-gone").read_bytes()

    async def gone(db, attachment_id):
        return False

    monkeypatch.setattr(media, "attachment_still_exists", gone)
    stats = await bf.run_backfill(published.db, execute=True, uid="hdrbf-gone")

    assert (stats.hdr, stats.vanished, stats.replaced, stats.failed) == (1, 1, 0, 0)
    assert bf.exit_code(stats) == 1
    assert poster.poster_path("hdrbf-gone").read_bytes() == kept  # no resurrected poster
    assert not poster.poster_tmp_path("hdrbf-gone").exists()  # the extraction was swept
    assert not bf.done_path("hdrbf-gone").exists()
    assert await published.blurhash_of("hdrbf-gone") == OLD_BLURHASH
