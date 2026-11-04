"""MEMNG-961 — 4 GiB uploads, admitted only when the part's volume can hold them.

`MAX_UPLOAD_MIB` defaults to 4096, so a session may declare up to 4 GiB. `POST /api/uploads` then
refuses with 507 when the volume that will hold the `.part` has less free space than the declared
size plus 10 % (`free < size * 1.1`): a refused session leaves no `uploads` row and no part file.
A staging dedupe hit (MEMNG-959) sends no bytes, so it is admitted whatever the free space.

The free-space probe is stubbed (`storage.free_bytes`); the route under test is real.
"""

from pathlib import Path

import pytest
from app.config import settings
from app.services import storage
from app.services import uploads as svc
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_upload_staging_dedupe import ONE, _ready_memo, _sha
from tests.test_upload_staging_dedupe import _cleanup as _cleanup_all

GIB = 1024 * 1024 * 1024


def _uploads() -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text("SELECT upload_uid FROM uploads"))]
    eng.dispose()
    return out


def _parts(data_root: Path) -> list[Path]:
    return sorted((data_root / "uploads").glob("*.part"))


class _Disk:
    """A stand-in for the free-space probe: answers `free` and records every root it was asked."""

    def __init__(self, free: int) -> None:
        self.free = free
        self.asked: list[Path] = []

    def __call__(self, root: Path) -> int:
        self.asked.append(Path(root))
        return self.free


@pytest.fixture
def disk(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    probe = _Disk(free=100 * GIB)
    monkeypatch.setattr(storage, "free_bytes", probe)
    try:
        yield probe
    finally:
        _cleanup_all()


def _open(client, size: int, **extra):
    return client.post(
        "/api/uploads", json={"filename": "big.mov", "mime": "video/mp4", "size": size, **extra}
    )


@pytest.mark.parametrize(
    ("size", "free"),
    [
        (1000, 1099),  # one byte under size * 1.1
        (1001, 1101),  # size * 1.1 = 1101.1: `size + size // 10` would wrongly admit this
        (4 * GIB, 4 * GIB),  # the whole file fits, the head-room does not
        (4 * GIB, 0),
    ],
)
def test_a_session_the_volume_cannot_hold_is_refused_with_507_and_leaves_nothing(
    logged_in_client, disk, tmp_path, size, free
):
    client, _ = logged_in_client
    disk.free = free

    r = _open(client, size)

    assert r.status_code == 507, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "space" in detail.lower(), detail
    assert _uploads() == []
    assert _parts(tmp_path) == []


@pytest.mark.parametrize(
    ("size", "free"),
    [
        (1000, 1100),  # exactly size * 1.1
        (1001, 1102),
        (4 * GIB, 4 * GIB + 4 * GIB // 10 + 1),
    ],
)
def test_a_session_the_volume_can_hold_is_admitted(logged_in_client, disk, tmp_path, size, free):
    client, _ = logged_in_client
    disk.free = free

    r = _open(client, size)

    assert r.status_code == 201, r.text
    uid = r.json()["upload_uid"]
    assert _uploads() == [(uid,)]
    assert _parts(tmp_path) == [tmp_path / "uploads" / f"{uid}.part"]


def test_the_probe_reads_the_volume_that_holds_the_part(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client

    assert _open(client, 1000).status_code == 201

    assert disk.asked == [tmp_path / "uploads"]


def test_a_staging_dedupe_hit_is_admitted_with_no_free_space(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client
    _ready_memo(client)  # the twin, uploaded while the volume had room
    disk.free = 0

    r = _open(client, len(ONE), sha256=_sha(ONE))

    assert r.status_code == 201, r.text
    assert r.json()["bytes_received"] == len(ONE)  # the hit: no byte will travel


def test_a_dedupe_miss_is_still_held_to_the_free_space_bound(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client
    disk.free = 0

    r = _open(client, len(ONE), sha256=_sha(ONE))  # no twin: the bytes must travel

    assert r.status_code == 507, r.text
    assert _uploads() == []
    assert _parts(tmp_path) == []


def test_the_limits_endpoint_publishes_4_gib(logged_in_client):
    client, _ = logged_in_client

    r = client.get("/api/uploads/limits")

    assert r.status_code == 200
    assert r.json()["max_bytes"] == 4096 * 1024 * 1024 == 4 * GIB


def test_a_session_of_exactly_4_gib_is_admitted_and_one_byte_more_is_413(logged_in_client, disk):
    client, _ = logged_in_client

    assert _open(client, 4 * GIB).status_code == 201
    over = _open(client, 4 * GIB + 1)
    assert over.status_code == 413, over.text
    assert disk.asked == [disk.asked[0]]  # the 413 is decided before the disk is asked


def test_the_507_is_declared_in_the_openapi_document():
    from app.main import app

    op = app.openapi()["paths"]["/api/uploads"]["post"]

    assert "507" in op["responses"]


@pytest.mark.parametrize(
    ("size", "free", "admitted"),
    [
        (0, 0, True),
        (1, 1, False),
        (1, 2, True),
        (10, 10, False),
        (10, 11, True),
        (1001, 1101, False),
        (1001, 1102, True),
        (4 * GIB, 4 * GIB + 429496729, False),  # 4 GiB * 1.1 = 4 GiB + 429496729.6
        (4 * GIB, 4 * GIB + 429496730, True),
    ],
)
def test_has_room_for_is_free_at_least_size_times_1_1(size, free, admitted):
    assert svc.has_room_for(size, free) is admitted


def test_the_refusal_reason_names_both_numbers():
    reason = str(svc.InsufficientSpace(size=2000, free=1500))

    assert "2000" in reason and "1500" in reason and "space" in reason
