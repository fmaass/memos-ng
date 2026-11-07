"""MEMNG-1202 — the MEMNG-961 disk admission does not stop trip filing.

Filing a pulled trip batch moves bytes that are already on the home volume: `_file_part` opens an
upload session per staged blob and RENAMES the staged file onto its part, so filing needs no new
space. The admission exists to refuse a session whose bytes have yet to arrive, and filing passes
`admit_space=False` to skip it. Without that, a volume under the head-room bound refused every
filing with `InsufficientSpace`, which `trip_ingest` does not catch, and the batch stayed open
until the disk freed up, although filing it needed no space at all.

The free-space probe the admission reads (`storage.free_bytes`) is forced to answer 0; the filing
and the upload route under test are real.
"""

from pathlib import Path

import pytest
from app.config import settings
from app.services import storage

from tests import test_trip_ingest_puller as tp
from tests.ingest_support import JPEG
from tests.test_upload_staging_dedupe import _cleanup as _cleanup_uploads

pluto_schema = tp.pluto_schema
world = tp.world


class _FullDisk:
    """The free-space probe of a volume with nothing left: answers 0, records each root asked."""

    def __init__(self) -> None:
        self.asked: list[Path] = []

    def __call__(self, root: Path) -> int:
        self.asked.append(Path(root))
        return 0


@pytest.mark.parametrize("count", [1, 2])
async def test_a_trip_batch_files_while_the_disk_admission_would_refuse(world, monkeypatch, count):
    trip = await tp._trip(world)
    token = await tp._guest(world, trip)
    files = [(JPEG + bytes([n]), f"{n}.jpg") for n in range(count)]
    batch = await tp._send(world, token, *files, caption="full disk")
    # Forced only now, for the filing tick: the guest door above has its own floor.
    full = _FullDisk()
    monkeypatch.setattr(storage, "free_bytes", full)
    assert await tp._tick(world) == "ok"
    assert tp._home_batch(batch) == [("acked", True, True, False)]
    [(memo_id, *_)] = tp._memos()
    rows = tp._sql(
        "SELECT filename, storage_path FROM attachments WHERE memo_id = :m ORDER BY filename",
        {"m": memo_id},
    )
    assert [(name, Path(path).read_bytes()) for name, path in rows] == [
        (name, data) for data, name in files
    ]
    assert full.asked == [], "filing consulted the admission it bypasses"
    assert tp._sql("SELECT count(*) FROM uploads")[0][0] == 0


def test_a_home_upload_under_the_same_full_disk_is_still_refused(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    full = _FullDisk()
    monkeypatch.setattr(storage, "free_bytes", full)
    try:
        r = client.post(
            "/api/uploads", json={"filename": "a.jpg", "mime": "image/jpeg", "size": len(JPEG)}
        )
        assert r.status_code == 507, r.text
        assert full.asked, "the home upload path no longer reads the admission's probe"
        assert sorted((tmp_path / "uploads").glob("*.part")) == []
    finally:
        _cleanup_uploads()
