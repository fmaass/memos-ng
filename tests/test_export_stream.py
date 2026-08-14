"""The streaming archive itself (app/services/export.py), driven directly.

The bounded-memory tests are the point of this file: they fail if the archive is ever
materialized before it is sent, which is exactly the regression the old EXPORT_MAX cap existed
to paper over. Nothing here is mocked — real zip bytes, real files on disk, real memory numbers.
"""

import base64
import io
import json
import os
import tracemalloc
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from app.services.export import CHUNK_SIZE, ExportItem, MediaFile, stream_zip

MTIME = datetime(2024, 1, 2, 3, 4, 6)  # even second: DOS timestamps have 2-second granularity


def _item(i: int, *, text: str = "alpha", media: tuple[MediaFile, ...] = ()) -> ExportItem:
    return ExportItem(
        arcname=f"m{i}.md",
        text=text,
        mtime=MTIME,
        manifest={"uid": f"m{i}"},
        media=media,
    )


async def _items(*items: ExportItem) -> AsyncIterator[ExportItem]:
    for item in items:
        yield item


async def _rows(n: int, size: int, produced: list[int]) -> AsyncIterator[ExportItem]:
    """n memos of ~`size` incompressible bytes each, generated one at a time."""
    for i in range(n):
        produced.append(i)
        yield _item(i, text=base64.urlsafe_b64encode(os.urandom(size)).decode())


async def _collect(source: AsyncIterator[ExportItem]) -> bytes:
    return b"".join([chunk async for chunk in stream_zip(source)])


async def _drain(source: AsyncIterator[ExportItem]) -> int:
    """Consume the archive without holding it — the shape a real download has."""
    total = 0
    async for chunk in stream_zip(source):
        total += len(chunk)
    return total


async def test_stream_zip_writes_entries_manifest_and_mtimes():
    blob = await _collect(_items(_item(0), _item(1, text="beta")))

    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.testzip() is None
    assert zf.namelist() == ["m0.md", "m1.md", "manifest.json"]
    assert zf.read("m0.md") == b"alpha"
    assert zf.read("m1.md") == b"beta"
    assert zf.getinfo("m0.md").date_time == MTIME.timetuple()[:6]
    assert [r["uid"] for r in json.loads(zf.read("manifest.json"))] == ["m0", "m1"]


async def test_stream_zip_with_no_rows_is_a_valid_empty_archive():
    zf = zipfile.ZipFile(io.BytesIO(await _collect(_items())))
    assert zf.namelist() == ["manifest.json"]
    assert json.loads(zf.read("manifest.json")) == []


async def test_media_payload_is_carried_with_its_bytes_and_file_mtime(tmp_path: Path):
    payload = os.urandom(CHUNK_SIZE * 3 + 17)  # spans several reads, ends mid-chunk
    src = tmp_path / "photo.jpg"
    src.write_bytes(payload)
    os.utime(src, (1_562_243_400, 1_562_243_400))  # 2019-07-04 12:30:00 UTC
    media = (MediaFile(uid="att1", arcname="media/m0/photo.jpg", path=src),)

    blob = await _collect(_items(_item(0, media=media)))

    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.testzip() is None
    assert zf.read("media/m0/photo.jpg") == payload
    # the file's own mtime rides along (media.py stamps derived tiers with captured_at)
    assert zf.getinfo("media/m0/photo.jpg").date_time[:3] == (2019, 7, 4)
    assert json.loads(zf.read("manifest.json"))[0]["media"] == [
        {"attachment": "att1", "file": "media/m0/photo.jpg", "size": len(payload)}
    ]


async def test_missing_media_is_skipped_with_a_manifest_note(tmp_path: Path):
    """Both flavours of absent: no file on disk, and no such tier for the attachment at all."""
    media = (
        MediaFile(uid="gone", arcname="media/m0/gone.jpg", path=tmp_path / "not-there.jpg"),
        MediaFile(uid="untiered", arcname="media/m0/untiered.jpg", path=None),
    )

    zf = zipfile.ZipFile(io.BytesIO(await _collect(_items(_item(0, media=media)))))

    assert zf.namelist() == ["m0.md", "manifest.json"]  # the export still completes
    assert json.loads(zf.read("manifest.json"))[0]["media"] == [
        {"attachment": "gone", "file": "media/m0/gone.jpg", "missing": True},
        {"attachment": "untiered", "file": "media/m0/untiered.jpg", "missing": True},
    ]


async def test_first_bytes_arrive_before_the_last_row_is_read():
    """The stream is produced as rows arrive: buffering the archive first would drain all 500."""
    produced: list[int] = []
    stream = stream_zip(_rows(500, 64, produced))
    try:
        first = await anext(stream)
    finally:
        await stream.aclose()

    assert first
    assert len(produced) == 1


async def test_large_text_export_stays_in_bounded_memory():
    """~16 MB of memo text through a peak of a few hundred KB. Buffering the archive (the old
    BytesIO) would put the whole thing in the peak instead."""
    produced: list[int] = []
    tracemalloc.start()
    try:
        total = await _drain(_rows(2000, 6000, produced))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(produced) == 2000
    assert total > 10_000_000
    assert peak < 4_000_000


async def test_large_media_payload_is_never_fully_read_into_memory(tmp_path: Path):
    """A single 24 MB attachment streams through the same few hundred KB of peak."""
    size = 24 * 1024 * 1024
    src = tmp_path / "big.mp4"
    src.write_bytes(os.urandom(size))
    media = (MediaFile(uid="big", arcname="media/m0/big.mp4", path=src),)

    tracemalloc.start()
    try:
        total = await _drain(_items(_item(0, media=media)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert total > size  # the payload really went through (stored, not compressed)
    assert peak < 4_000_000
