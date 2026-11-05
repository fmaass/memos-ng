"""MEMNG-1175 — a volume too full to make the uploads directory is a 507, not a 500.

`storage.free_bytes` makes the directory it measures before measuring it. On a running install
that directory exists, but when it does not and the volume is too full to create it, `mkdir`
raises `OSError(ENOSPC)`. That error is the volume's own answer to the admission's question —
there is no room — so the probe reports 0 free bytes, and each caller's existing refusal answers:
the household session's `InsufficientSpace` (507 with its human-readable reason, per entry in a
batch) and the guest door's floor (507). Any other OSError — a permission or I/O fault — is a
real error and still propagates.

Only `Path.mkdir` is stubbed, and only for the one directory the probe makes: the probe, the
admission and the routes under test are real.
"""

import errno
import os
import shutil
from pathlib import Path

import pytest
from app.config import settings
from app.ingest import core
from app.services import storage
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.ingest_support import (
    bearer,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    redeem,
    seeded_pass,
)
from tests.test_upload_staging_dedupe import _cleanup as _cleanup_all

BATCH = "/api/uploads/batch"
_REAL_MKDIR = Path.mkdir


def _refuse_mkdir(monkeypatch, target: Path, code: int = errno.ENOSPC, message: str = "") -> None:
    """Make `mkdir` of the ABSENT `target` fail as a full volume does; every other mkdir is real.
    An existing `target` is left to the real call, which `exist_ok` already answers."""

    def mkdir(self: Path, *args, **kwargs) -> None:
        if self == target and not self.exists():
            raise OSError(code, message or os.strerror(code), str(self))
        _REAL_MKDIR(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)


def _uploads() -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text("SELECT filename FROM uploads ORDER BY filename"))]
    eng.dispose()
    return out


@pytest.fixture
def full_volume(monkeypatch, tmp_path):
    """DATA_ROOT with no `uploads` directory, on a volume that refuses to make one."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    uploads = tmp_path / "uploads"
    assert not uploads.exists()
    _refuse_mkdir(monkeypatch, uploads)
    _cleanup_all()
    try:
        yield uploads
    finally:
        _cleanup_all()


# --- the probe ---------------------------------------------------------------------------------


def test_a_volume_too_full_to_make_the_directory_has_no_free_bytes(monkeypatch, tmp_path):
    root = tmp_path / "uploads"
    _refuse_mkdir(monkeypatch, root)

    assert storage.free_bytes(root) == 0
    assert not root.exists()


def test_the_probe_still_measures_a_directory_it_can_make(tmp_path):
    root = tmp_path / "a" / "uploads"

    assert storage.free_bytes(root) > 0
    assert root.is_dir()


@pytest.mark.parametrize("code", [errno.EACCES, errno.EROFS, errno.EIO])
def test_any_other_mkdir_failure_is_still_a_real_error(monkeypatch, tmp_path, code):
    root = tmp_path / "uploads"
    _refuse_mkdir(monkeypatch, root, code)

    with pytest.raises(OSError) as raised:
        storage.free_bytes(root)
    assert raised.value.errno == code


def test_the_errno_decides_not_the_message(monkeypatch, tmp_path):
    # An I/O fault whose text reads like a full disk is still an I/O fault.
    root = tmp_path / "uploads"
    _refuse_mkdir(monkeypatch, root, errno.EIO, os.strerror(errno.ENOSPC))

    with pytest.raises(OSError) as raised:
        storage.free_bytes(root)
    assert raised.value.errno == errno.EIO


# --- the household upload session ----------------------------------------------------------------


def test_a_single_create_is_refused_507_with_the_admissions_reason(logged_in_client, full_volume):
    client, _ = logged_in_client

    r = client.post("/api/uploads", json={"filename": "a.jpg", "mime": "image/jpeg", "size": 1})

    assert r.status_code == 507, r.text
    assert r.json()["detail"] == (
        "Not enough free space on the server for this upload: it needs 1 bytes plus 10 % "
        "head-room, and 0 bytes are free. Try again later."
    )
    assert _uploads() == []
    assert not full_volume.exists()


def test_every_batch_entry_is_refused_507_on_its_own(logged_in_client, full_volume):
    client, _ = logged_in_client
    entries = [
        {"filename": "a.jpg", "mime": "image/jpeg", "size": 10},
        {"filename": "b.mov", "mime": "video/mp4", "size": 4000},
    ]

    r = client.post(BATCH, json={"entries": entries})

    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["status"] for x in results] == [507, 507]
    assert [x["detail"] for x in results] == [
        "Not enough free space on the server for this upload: it needs 10 bytes plus 10 % "
        "head-room, and 0 bytes are free. Try again later.",
        "Not enough free space on the server for this upload: it needs 4000 bytes plus 10 % "
        "head-room, and 0 bytes are free. Try again later.",
    ]
    assert _uploads() == []
    assert not full_volume.exists()


def test_a_retry_once_the_volume_has_room_opens_the_session(
    logged_in_client, full_volume, monkeypatch
):
    client, _ = logged_in_client
    body = {"filename": "a.jpg", "mime": "image/jpeg", "size": 1}
    assert client.post("/api/uploads", json=body).status_code == 507

    monkeypatch.setattr(Path, "mkdir", _REAL_MKDIR)  # the volume has room again
    r = client.post("/api/uploads", json=body)

    assert r.status_code == 201, r.text
    assert _uploads() == [("a.jpg",)]  # the refusal left nothing the retry trips over
    assert (full_volume / f"{r.json()['upload_uid']}.part").exists()


# --- the guest door shares the probe --------------------------------------------------------------


async def test_the_guest_door_answers_its_own_507_not_a_500(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "INGEST_MIN_FREE_BYTES", 1)
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    root = core.ingest_root()
    shutil.rmtree(root, ignore_errors=True)
    _refuse_mkdir(monkeypatch, root)

    r = await door.post(
        "/api/guest/uploads", json={"filename": "a.jpg", "size": 1}, headers=bearer(token)
    )

    assert r.status_code == 507, r.text
    assert r.json()["detail"] == "The door is full; try again later"
    assert not root.exists()
