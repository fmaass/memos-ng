"""MEMNG-143 — nothing may RENAME a file into `data/assets` from outside it.

In the deployed stack `/app/data/assets` (derived tiers, NVMe) is a separate mount sitting inside
`/app/data` (originals and upload parts, HDD share). A rename across that boundary raises OSError
EXDEV in production and succeeds in every test environment, which is why the first real poster
override 500'd in the field with a green suite behind it. That one site is fixed; this module pins
the CLASS.

Two guards, because neither alone is enough:

* the STATIC one below enumerates every rename in `app/` and demands a written disposition for
  each, so a new site cannot appear unreviewed even if no test happens to exercise it;
* the LIVE ones drive the real tier writers with the mount boundary simulated for the WHOLE assets
  directory (`split_assets_mount`), so a placement that starts crossing it fails here rather than
  in production.
"""

import ast
from pathlib import Path

import pytest
from app.config import settings
from app.models import Attachment, AttachmentStatus, JobLane, JobState, MediaJob
from app.services import media, poster, printready
from PIL import Image

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Every call that moves a file by renaming it. `shutil.move` is included because it LOOKS like a
# rename and would be the obvious way to add one; it happens to fall back to a copy on EXDEV, but
# that is a property of shutil, not a licence to point one at the assets tree unreviewed.
RENAME_CALLS = {"replace", "rename", "move"}

# Every rename site in app/, keyed by (module, enclosing function) → why it cannot cross the
# boundary. Adding a rename means adding a line here, which is the review this guard exists to
# force. "sibling tmp" means the temp file is created INSIDE the destination directory, so the
# rename is within-device by construction whichever mount that directory is on.
DISPOSITIONS = {
    ("services/storage.py", "restore"): "originals only — a memo folder or _unfiled back to a part",
    ("services/storage.py", "move"): "originals only — a journaled _unfiled into a memo folder",
    ("services/trip_ingest.py", "_walk_back_sync"): "uploads and originals only — a journaled "
    "filing move walked back, each side on the data volume",
    ("services/storage.py", "_place_master_sync"): "originals only — _unfiled into a memo folder",
    ("services/storage.py", "_replace_master_sync"): "originals only — onto a print master "
    "already in its memo folder",
    ("services/uploads.py", "_move_into_sync"): "originals only — an upload part into originals",
    ("services/uploads.py", "_copy_into_sync"): "sibling tmp — THE poster-override fix: copy into "
    "a tmp in the destination directory, then rename within it",
    ("services/media.py", "_atomic_save"): "sibling tmp — thumbs/mobile/printready encode",
    ("services/media.py", "process_video_poster"): "sibling tmp — poster.poster_tmp_path",
    ("services/media.py", "process_slow"): "sibling tmp — the web mp4 beside its own destination",
    ("services/trip_ingest.py", "_replace_sync"): "uploads only — a staged trip download "
    "(data/uploads/trip-ingest) into its upload part",
    ("services/trip_ingest.py", "_restore"): "uploads only — an upload part back to trip-ingest "
    "staging; the originals go back through storage.restore",
    ("routers/assets.py", "_save_print_sync"): "data/print, not assets — a spooled print master",
    ("scripts/backfill_hdr_posters.py", "publish"): "sibling tmp — poster.poster_tmp_path, the "
    "same in-flight name process_video_poster renames from; the backup it takes first is a COPY "
    "out of assets, never a rename into it",
    ("scripts/backfill_hdr_posters.py", "mark_done"): "sibling tmp — the marker renamed beside "
    "its own destination inside data/backfill, outside assets entirely",
}


class _RenameWalk(ast.NodeVisitor):
    """Collect (module, enclosing function) for every `os.replace`/`os.rename`/`shutil.move`."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scopes: list[str] = []
        self.found: set[tuple[str, str]] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in RENAME_CALLS:
            owner = func.value
            # os.replace / os.rename / shutil.move — never `some_string.replace(...)`.
            if isinstance(owner, ast.Name) and owner.id in {"os", "shutil"}:
                self.found.add((self.rel, self.scopes[-1] if self.scopes else "<module>"))
        self.generic_visit(node)


def _rename_sites() -> set[tuple[str, str]]:
    """(module, enclosing function) for every rename call under app/ — an AST walk, so a name in a
    comment or a string cannot register and `str.replace` cannot be mistaken for `os.replace`."""
    found: set[tuple[str, str]] = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        walk = _RenameWalk(str(path.relative_to(APP_DIR)))
        walk.visit(ast.parse(path.read_text(), filename=str(path)))
        found |= walk.found
    return found


def test_every_rename_in_the_app_has_a_boundary_disposition():
    sites = _rename_sites()
    assert sites, "the AST scan found no renames at all — it has stopped working"
    undocumented = sites - set(DISPOSITIONS)
    assert not undocumented, (
        "new rename site(s) with no assets-boundary disposition: "
        f"{sorted(undocumented)} — state in DISPOSITIONS why this rename can never move a file "
        "into data/assets from another mount, or make it a copy-then-rename like _copy_into_sync"
    )
    stale = set(DISPOSITIONS) - sites
    assert not stale, f"DISPOSITIONS names rename site(s) that no longer exist: {sorted(stale)}"


def _png(path: Path, size=(900, 600)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 90, 160)).save(path, "PNG")


async def _seed_image(adb, tmp_path, uid: str, lane=JobLane.fast) -> tuple[Attachment, MediaJob]:
    orig = tmp_path / "originals" / "2026" / "08" / f"{uid}.png"
    _png(orig)
    att = Attachment(
        uid=uid,
        filename=f"{uid}.png",
        mime="image/png",
        size=orig.stat().st_size,
        status=AttachmentStatus.processing,
        storage_path=str(orig),
    )
    adb.add(att)
    await adb.flush()
    job = MediaJob(attachment_id=att.id, lane=lane, state=JobState.queued)
    adb.add(job)
    await adb.flush()
    return att, job


def test_the_fixture_really_refuses_a_rename_across_the_boundary(split_assets_mount, tmp_path):
    """The positive control for every test below: without this, a green run would also be what a
    fixture that quietly does nothing produces."""
    import os

    split_assets_mount(tmp_path)
    outside = tmp_path / "originals" / "x.bin"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"x")
    inside = tmp_path / "assets" / "thumbs" / "x.bin"
    inside.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(OSError) as exc:
        os.replace(outside, inside)
    assert "cross-device" in str(exc.value)

    # …and a rename that stays on one side still works, so the guard is not simply breaking renames.
    sibling = inside.with_suffix(".tmp")
    sibling.write_bytes(b"y")
    os.replace(sibling, inside)
    assert inside.read_bytes() == b"y"
    os.replace(outside, outside.with_suffix(".moved"))


async def test_the_fast_lane_writes_its_tiers_across_the_mount_split(
    adb, monkeypatch, tmp_path, split_assets_mount
):
    """thumbs + mobile: `_atomic_save` encodes to a sibling temp, so the publish never crosses."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    split_assets_mount(tmp_path)
    att, job = await _seed_image(adb, tmp_path, "splitimg")

    await media.process_job(adb, job)

    assert (tmp_path / "assets" / "thumbs" / "splitimg.jpg").exists()
    assert (tmp_path / "assets" / "mobile" / "splitimg.jpg").exists()
    assert att.status is AttachmentStatus.ready


def test_the_printready_rendition_lands_across_the_mount_split(
    monkeypatch, tmp_path, split_assets_mount
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    split_assets_mount(tmp_path)
    src = tmp_path / "originals" / "2026" / "08" / "big.png"
    _png(src, size=(2400, 1600))
    att = Attachment(
        uid="splitprint",
        filename="big.png",
        mime="image/png",
        size=src.stat().st_size,
        status=AttachmentStatus.ready,
        storage_path=str(src),
    )

    media.process_printready(att)

    assert printready.printready_path("splitprint").exists()


def test_the_poster_frame_lands_across_the_mount_split(monkeypatch, tmp_path, split_assets_mount):
    """The worker's own poster write, the sibling of the app-side override that broke in the field:
    ffmpeg writes the tmp inside `assets/poster/`, so the publish stays on that mount."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    split_assets_mount(tmp_path)
    src = tmp_path / "originals" / "2026" / "08" / "clip.mov"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not really a container")

    def fake_ffprobe(_path):
        return {"width": 640, "height": 480, "duration": 3.0}

    def fake_run(cmd, **kwargs):
        # Stand in for ffmpeg: write the frame to the output path the command names, which is
        # `poster_tmp_path` — the rename that follows is the thing under test.
        Image.new("RGB", (64, 48), (9, 9, 9)).save(cmd[cmd.index("-y") + 1 :][-1], "JPEG")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(media, "_ffprobe", fake_ffprobe)
    monkeypatch.setattr(media.subprocess, "run", fake_run)
    att = Attachment(
        uid="splitposter",
        filename="clip.mov",
        mime="video/quicktime",
        size=1,
        status=AttachmentStatus.pending,
        storage_path=str(src),
    )

    media.process_video_poster(att)

    assert poster.poster_path("splitposter").exists()
    assert not poster.poster_tmp_path("splitposter").exists()
