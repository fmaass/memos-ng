"""Tests for the one-shot #curated → aura-frames album backfill.

Two layers:
* ``_upload_targets`` — the render+upload loop, exercised with fakes (no DB, no Pillow, no album)
  so dry-run safety, per-item failure isolation, the ``.jpg`` name, the memo-date passthrough, and
  the missing-file skip are asserted directly.
* ``load_curated_targets`` — the selection SQL, exercised against the real migrated schema (``adb``)
  so the "screen only" contract is proven per input category: a raster image is selected; an svg
  image, a video, a print rendition, and a non-#curated memo's image are all excluded.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from app.config import settings
from app.models import (
    Attachment,
    AttachmentRendition,
    AttachmentStatus,
    Memo,
    Tag,
    User,
    Visibility,
)
from app.scripts import backfill_frame_album as bf
from app.services import frame_share as fs
from app.services.frame_share import CURATED_TAG

MEMO_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def _target(idx: int, storage_path, *, filename: str = "photo.png", memo_id: int = 0):
    memo = SimpleNamespace(id=memo_id or idx, created_at=MEMO_DATE)
    att = SimpleNamespace(id=idx, storage_path=str(storage_path), filename=filename)
    return memo, att


class _Upload:
    """Records every album upload; raises for filenames in ``fail_on`` to test isolation."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.fail_on = fail_on or set()

    async def __call__(self, filename: str, jpeg: bytes, *, source_ref: str | None = None) -> None:
        self.calls.append((filename, jpeg))
        if filename in self.fail_on:
            raise RuntimeError("simulated upload failure")


class _Render:
    """Records (path, photo_date) it was called with; returns deterministic bytes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, path: str, photo_date) -> bytes:
        self.calls.append((path, photo_date))
        return b"JPEG:" + path.encode()


# --- _upload_targets: the render + upload loop -------------------------------------------------


async def test_dry_run_counts_but_uploads_nothing(tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    targets = [
        _target(1, tmp_path / "a.png", memo_id=10),
        _target(2, tmp_path / "b.png", memo_id=10),
    ]
    up, rr = _Upload(), _Render()
    stats = await bf._upload_targets(targets, execute=False, render=rr, upload=up)
    assert up.calls == [] and rr.calls == []  # a dry run must never render or upload
    assert stats.uploaded == 0
    assert stats.curated_image_attachments == 2
    assert stats.curated_memos_with_images == 1  # both photos belong to memo 10


async def test_execute_uploads_with_jpg_name_and_memo_date(tmp_path):
    (tmp_path / "café.png").write_bytes(b"x")
    targets = [_target(1, tmp_path / "café.png", filename="café.png", memo_id=5)]
    up, rr = _Upload(), _Render()
    stats = await bf._upload_targets(targets, execute=True, render=rr, upload=up)
    assert stats.uploaded == 1 and stats.failed == 0
    ((name, jpeg),) = up.calls
    assert name == "café.jpg"  # the screen render is a .jpg named from the original stem
    assert rr.calls == [(str(tmp_path / "café.png"), MEMO_DATE)]  # photo_date = memo.created_at
    assert jpeg == b"JPEG:" + str(tmp_path / "café.png").encode()


async def test_execute_sends_the_attachment_source_ref_header(tmp_path, monkeypatch):
    # The script's own default uploader against aura-frames at the HTTP boundary: a backfilled
    # photo must carry the same X-Source-Ref as the worker's upload of the same attachment.
    monkeypatch.setattr(settings, "FRAME_API_URL", "http://aura-frames.test")
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", "tok")
    monkeypatch.setattr(settings, "FRAME_ALBUM_ID", 12)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"status": "created", "id": 1})

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        fs.httpx, "AsyncClient", lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    )
    (tmp_path / "p.png").write_bytes(b"x")
    targets = [_target(77, tmp_path / "p.png", filename="p.png", memo_id=5)]
    stats = await bf._upload_targets(
        targets, execute=True, render=_Render(), upload=bf.upload_to_album
    )
    assert stats.uploaded == 1 and stats.failed == 0
    assert [r.headers.get("X-Source-Ref") for r in captured] == ["memos-attachment:77"]


async def test_missing_or_empty_file_is_skipped_not_uploaded(tmp_path):
    targets = [
        _target(1, tmp_path / "gone.png", memo_id=1),  # path never created on disk
        _target(2, "", memo_id=2),  # no storage_path at all
    ]
    up, rr = _Upload(), _Render()
    stats = await bf._upload_targets(targets, execute=True, render=rr, upload=up)
    assert stats.skipped_missing_file == 2
    assert stats.uploaded == 0 and up.calls == [] and rr.calls == []


async def test_one_upload_failure_does_not_abort_the_run(tmp_path):
    for i in range(3):
        (tmp_path / f"{i}.png").write_bytes(b"x")
    targets = [_target(i, tmp_path / f"{i}.png", filename=f"{i}.png", memo_id=1) for i in range(3)]
    up = _Upload(fail_on={"1.jpg"})  # the middle photo's upload raises
    stats = await bf._upload_targets(targets, execute=True, render=_Render(), upload=up)
    assert stats.uploaded == 2 and stats.failed == 1
    assert len(up.calls) == 3  # the failing item was attempted AND the third still processed


# --- load_curated_targets: the selection SQL against the real schema ---------------------------


async def test_load_curated_targets_selects_only_curated_screen_images(adb):
    user = User(subject="alice", role="user")
    adb.add(user)
    await adb.flush()

    curated = Tag(name=CURATED_TAG)
    adb.add(curated)
    await adb.flush()

    memo = Memo(uid="m1", owner_id=user.id, visibility=Visibility.PUBLIC)
    memo.tags.append(curated)
    adb.add(memo)
    await adb.flush()

    def _att(uid: str, mime: str, path: str) -> Attachment:
        return Attachment(
            uid=uid,
            owner_id=user.id,
            memo_id=memo.id,
            filename=f"{uid}",
            mime=mime,
            size=1,
            status=AttachmentStatus.ready,
            storage_path=path,
        )

    jpeg = _att("a-jpg", "image/jpeg", "/data/a.jpg")  # selected
    png = _att("a-png", "image/png", "/data/a.png")  # selected
    svg = _att("a-svg", "image/svg+xml", "/data/a.svg")  # excluded (not a raster photo)
    video = _att("a-mp4", "video/mp4", "/data/a.mp4")  # excluded (not an image)
    adb.add_all([jpeg, png, svg, video])
    await adb.flush()

    # a print master for the screen jpeg — a hidden rendition + the legacy inline print fields.
    # Neither is an Attachment row, so neither may appear as a target.
    jpeg.print_path = "/data/print/a-legacy.tif"
    jpeg.print_mime = "image/tiff"
    adb.add(
        AttachmentRendition(
            attachment_id=jpeg.id,
            variant_key="print",
            filename="a-print.tif",
            mime="image/tiff",
            size=1,
            storage_path="/data/print/a.tif",
        )
    )

    # a non-#curated memo with an image — must be excluded entirely
    other = Memo(uid="m2", owner_id=user.id, visibility=Visibility.PUBLIC)
    adb.add(other)
    await adb.flush()
    adb.add(
        Attachment(
            uid="a-other",
            owner_id=user.id,
            memo_id=other.id,
            filename="other.jpg",
            mime="image/jpeg",
            size=1,
            status=AttachmentStatus.ready,
            storage_path="/data/o.jpg",
        )
    )
    await adb.flush()

    targets = await bf.load_curated_targets(adb)
    assert [(m.id, a.id) for m, a in targets] == [(memo.id, jpeg.id), (memo.id, png.id)]
