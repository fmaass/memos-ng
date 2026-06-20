import io
import json
import zipfile
from datetime import datetime


def _make(client, content, visibility):
    assert (
        client.post("/api/memos", json={"content": content, "visibility": visibility}).status_code
        == 201
    )


def test_export_zip_matches_filter_and_mtimes(logged_in_client):
    client, _ = logged_in_client
    _make(client, "alpha #x", "PUBLIC")
    _make(client, "beta", "PUBLIC")
    _make(client, "secret", "PRIVATE")  # excluded by visibility=PUBLIC

    r = client.get("/api/export?visibility=PUBLIC")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
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


def test_export_too_large_returns_413(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    monkeypatch.setattr("app.routers.export.EXPORT_MAX", 1)
    _make(client, "a", "PUBLIC")
    _make(client, "b", "PUBLIC")
    r = client.get("/api/export?visibility=PUBLIC")
    assert r.status_code == 413 and "too large" in r.json()["detail"].lower()
