import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import get_args

from app.config import settings
from app.models import Attachment
from app.routers import assets
from app.routers.export import ExportTier, _media_name, _unique_name
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL


def _make(client, content, visibility) -> dict:
    r = client.post("/api/memos", json={"content": content, "visibility": visibility})
    assert r.status_code == 201
    return r.json()


def _attach(memo_uid, att_uid, subject, *, filename, storage_path, mime="image/jpeg"):
    """Link a ready attachment to a memo (the upload pipeline is exercised in test_uploads)."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        oid = c.execute(
            text("SELECT id FROM users WHERE subject=:s"), {"s": subject}
        ).scalar_one()
        mid = c.execute(text("SELECT id FROM memos WHERE uid=:u"), {"u": memo_uid}).scalar_one()
        aid = c.execute(
            text(
                "INSERT INTO attachments "
                "(uid, owner_id, memo_id, filename, mime, size, status, storage_path) "
                "VALUES (:u,:o,:m,:f,:mi,:sz,CAST('ready' AS attachment_status),:sp) RETURNING id"
            ),
            {
                "u": att_uid,
                "o": oid,
                "m": mid,
                "f": filename,
                "mi": mime,
                "sz": os.path.getsize(storage_path) if os.path.exists(storage_path) else 0,
                "sp": str(storage_path),
            },
        ).scalar_one()
    eng.dispose()
    return aid


def _cleanup(*aids):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for aid in aids:
            c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    eng.dispose()


def _zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert zf.testzip() is None
    return zf


def test_export_zip_matches_filter_and_mtimes(logged_in_client):
    client, _ = logged_in_client
    _make(client, "alpha #x", "PUBLIC")
    _make(client, "beta", "PUBLIC")
    _make(client, "secret", "PRIVATE")  # excluded by visibility=PUBLIC

    zf = _zip(client.get("/api/export?visibility=PUBLIC"))
    names = zf.namelist()
    md = [n for n in names if n.endswith(".md")]
    assert len(md) == 2 and "manifest.json" in names  # exactly the 2 PUBLIC memos

    manifest = json.loads(zf.read("manifest.json"))
    first = manifest[0]
    info = zf.getinfo(f"{first['uid']}.md")
    ca = datetime.fromisoformat(first["created_at"])
    # zip entry mtime is stamped from the memo's created_at (captured_at analogue)
    assert info.date_time[:5] == (ca.year, ca.month, ca.day, ca.hour, ca.minute)


def test_export_requires_auth(client):
    assert client.get("/api/export").status_code == 401


def test_export_has_no_size_cap_and_pages_through_batches(logged_in_client, monkeypatch):
    """Every matching memo is exported however many batches it takes — no cap, no 413."""
    monkeypatch.setattr("app.routers.export.BATCH_SIZE", 1)
    client, _ = logged_in_client
    for i in range(5):
        _make(client, f"memo {i}", "PUBLIC")

    zf = _zip(client.get("/api/export?visibility=PUBLIC"))

    assert len([n for n in zf.namelist() if n.endswith(".md")]) == 5
    assert len(json.loads(zf.read("manifest.json"))) == 5


def test_export_carries_media_payload_bytes(logged_in_client, tmp_path):
    client, subject = logged_in_client
    memo = _make(client, "with a photo", "PUBLIC")
    payload = os.urandom(300_000)
    src = tmp_path / "IMG_0042.jpg"
    src.write_bytes(payload)
    os.utime(src, (1_562_243_400, 1_562_243_400))  # 2019-07-04 12:30:00 UTC
    aid = _attach(memo["uid"], "att-exp-1", subject, filename="IMG_0042.jpg", storage_path=src)
    try:
        zf = _zip(client.get("/api/export?include_media=true&tier=original"))
    finally:
        _cleanup(aid)

    name = f"media/{memo['uid']}/IMG_0042.jpg"
    assert zf.read(name) == payload
    assert zf.getinfo(name).date_time[:3] == (2019, 7, 4)  # the file's own mtime
    record = next(r for r in json.loads(zf.read("manifest.json")) if r["uid"] == memo["uid"])
    assert record["media"] == [
        {"attachment": "att-exp-1", "file": name, "size": len(payload)}
    ]


def test_export_notes_a_tier_that_is_not_on_disk(logged_in_client, tmp_path, monkeypatch):
    """A rendition the worker hasn't produced yet must not fail the whole export."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    client, subject = logged_in_client
    memo = _make(client, "photo without a thumb", "PUBLIC")
    src = tmp_path / "IMG_0043.heic"
    src.write_bytes(b"original bytes")
    aid = _attach(memo["uid"], "att-exp-2", subject, filename="IMG_0043.heic", storage_path=src)
    try:
        zf = _zip(client.get("/api/export?include_media=true&tier=thumb"))
    finally:
        _cleanup(aid)

    assert f"{memo['uid']}.md" in zf.namelist()
    assert not [n for n in zf.namelist() if n.startswith("media/")]
    record = next(r for r in json.loads(zf.read("manifest.json")) if r["uid"] == memo["uid"])
    # named for the tier that was asked for: a thumb of an .heic is a .jpg
    assert record["media"] == [
        {
            "attachment": "att-exp-2",
            "file": f"media/{memo['uid']}/IMG_0043.jpg",
            "missing": True,
        }
    ]


def test_export_without_include_media_carries_only_text(logged_in_client, tmp_path):
    client, subject = logged_in_client
    memo = _make(client, "with a photo", "PUBLIC")
    src = tmp_path / "IMG_0044.jpg"
    src.write_bytes(b"jpeg bytes")
    aid = _attach(memo["uid"], "att-exp-3", subject, filename="IMG_0044.jpg", storage_path=src)
    try:
        zf = _zip(client.get("/api/export"))
    finally:
        _cleanup(aid)

    assert not [n for n in zf.namelist() if n.startswith("media/")]
    record = next(r for r in json.loads(zf.read("manifest.json")) if r["uid"] == memo["uid"])
    assert "media" not in record


def test_export_rejects_an_unknown_tier(logged_in_client):
    client, _ = logged_in_client
    assert client.get("/api/export?include_media=true&tier=nope").status_code == 422


def test_export_tiers_are_the_asset_tiers():
    """The export offers exactly the renditions the asset routes serve — no drift."""
    assert set(get_args(ExportTier)) == set(assets.TIERS)


def _att(filename: str) -> Attachment:
    return Attachment(uid="a", filename=filename, mime="image/jpeg", size=1)


def test_media_name_keeps_the_original_filename():
    original = Path("/data/originals/2026/06/x/IMG_1.jpg")
    assert _media_name(_att("IMG_1.jpg"), original) == "IMG_1.jpg"


def test_media_name_takes_the_derived_tiers_extension():
    assert _media_name(_att("IMG_1.heic"), Path("/data/assets/thumbs/a.jpg")) == "IMG_1.jpg"


def test_media_name_falls_back_to_the_filename_when_the_tier_has_no_path():
    assert _media_name(_att("IMG_1.heic"), None) == "IMG_1.heic"


def test_media_name_sanitizes_a_client_filename():
    assert _media_name(_att("../../etc/passwd"), None) == "passwd"


def test_unique_name_leaves_a_free_name_alone():
    assert _unique_name(set(), "IMG_1.jpg") == "IMG_1.jpg"


def test_unique_name_bumps_collisions():
    used: set[str] = set()
    assert [_unique_name(used, "IMG_1.jpg") for _ in range(3)] == [
        "IMG_1.jpg",
        "IMG_1 (2).jpg",
        "IMG_1 (3).jpg",
    ]
