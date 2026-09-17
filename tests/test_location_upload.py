"""Uploading a geotagged file through the REAL API stores its coordinates on the attachment row.

One case per media family, because each one reaches the coordinates by a different route — a JPEG
and a HEIC through `im.info["exif"]`, a PNG through the eXIf chunk walk, a TIFF through `tag_v2`,
a MOV through `moov/meta` and an MP4 through `moov/udta/©xyz`. A family that regressed would
otherwise hide behind the five that did not.

The expected values come from `fixtures/locations.json`, the same artefact the reader is held to,
so this file cannot quietly disagree with it about what a fixture contains.
"""

import json
from pathlib import Path

import pytest
from app.config import settings
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

REPO = Path(__file__).resolve().parent.parent
BINARIES = REPO / "fixtures" / "locations"
ARTEFACT = json.loads((REPO / "fixtures/locations.json").read_text())

# One per family, plus the file that is read and carries nothing.
FAMILIES = [
    "jpeg-north-east.jpg",
    "jpeg-south-west.jpg",
    "png-exif-after-idat.png",
    "tiff-gps.tif",
    "heic-gps.heic",
    "mov-iso6709.mov",
    "mp4-xyz.mp4",
    "jpeg-no-gps.jpg",
]


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM frame_share_jobs"))
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


def _upload(client, name, mime, data: bytes) -> str:
    opened = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    return uid


def _stored(attachment_uid: str):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT latitude, longitude, location_source FROM attachments WHERE uid = :u"),
            {"u": attachment_uid},
        ).one()
    eng.dispose()
    return row


@pytest.mark.parametrize("name", FAMILIES)
def test_an_uploaded_file_stores_what_its_own_metadata_said(
    name, logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    spec = ARTEFACT["binaries"][name]
    try:
        upload_uid = _upload(client, name, spec["mime"], (BINARIES / name).read_bytes())
        created = client.post(
            "/api/memos",
            json={"visibility": "PUBLIC", "content": name, "upload_uids": [upload_uid]},
        )
        assert created.status_code == 201, created.text
        latitude, longitude, source = _stored(created.json()["attachments"][0]["uid"])
        assert {
            "latitude": latitude,
            "longitude": longitude,
            "location_source": source,
        } == spec["expect"]
    finally:
        _cleanup()


def test_an_unreadable_original_settles_as_retryable_rather_than_as_no_location(
    logged_in_client, monkeypatch, tmp_path
):
    # 'none' and 'unreadable' are different facts: the first is final, the second is what
    # `backfill_location.py --retry-unreadable` comes back for. A read that blew up must never be
    # recorded as "this photo was never geotagged".
    from app.services import location, uploads

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    def _boom(path, mime):
        raise OSError("the mount is down")

    monkeypatch.setattr(uploads.location, "read_location", _boom)
    try:
        payload = (BINARIES / "jpeg-north-east.jpg").read_bytes()
        upload_uid = _upload(client, "geo.jpg", "image/jpeg", payload)
        created = client.post(
            "/api/memos",
            json={"visibility": "PUBLIC", "content": "still stored", "upload_uids": [upload_uid]},
        )
        # The upload is NOT refused: a stored photo with no pin beats a rejected upload.
        assert created.status_code == 201, created.text
        assert _stored(created.json()["attachments"][0]["uid"]) == (
            None,
            None,
            location.SOURCE_UNREADABLE,
        )
    finally:
        _cleanup()
