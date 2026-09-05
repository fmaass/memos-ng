"""Acceptance probe for W-AUTODATE: the capture inspection endpoint over the real upload lane."""

import json
from pathlib import Path

from app.config import settings
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "auto-memo-date"
# The expected answer is the artefact's, by name — the probe proves the LANE, not the rule.
EXPECTED = json.loads((ROOT / "fixtures" / "auto-memo-date.json").read_text())["binaries"]


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


def _upload(client, name, mime):
    data = (FIXTURES / name).read_bytes()
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["upload_uid"]
    client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    return uid


def test_capture_endpoint_reports_the_jpeg_exif_instant(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _upload(client, "jpeg-offset.jpg", "image/jpeg")
        r = client.get(f"/api/uploads/{uid}/capture")
        assert r.status_code == 200, r.text
        assert r.json() == {"state": "done", **EXPECTED["jpeg-offset.jpg"]["expect"]}
    finally:
        _cleanup()
