from app.config import settings
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM attachment_renditions"))
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
        c.execute(text("DELETE FROM memos"))
    eng.dispose()


def _put(client, name, data, mime="image/png"):
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert r.status_code == 201
    uid = r.json()["upload_uid"]
    client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    return uid


def test_memo_with_screen_and_print_groups_and_exposes_via_api(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        # dump the print BEFORE the screen — the two-pass grouping must still bind them
        glossy = _put(client, "_AR1 - print (glossy).png", PNG + b"g" * 4000)
        screen = _put(client, "_AR1 - screen.png", PNG + b"s" * 3000)
        memo = client.post(
            "/api/memos", json={"content": "shot", "upload_uids": [glossy, screen]}
        ).json()

        # the memo shows exactly ONE photo (the screen); the print is hidden
        assert len(memo["attachments"]) == 1
        att = memo["attachments"][0]
        assert att["hasPrint"] is True
        assert att["hasLegacyPrint"] is False  # renditions-only → legacy /print button hides
        assert len(att["renditions"]) == 1
        r = att["renditions"][0]
        assert r["variant_key"] == "print:glossy" and r["label"] == "glossy"
        assert r["downloadUrl"] == f"/api/attachments/{att['uid']}/rendition/print:glossy"

        # the list endpoint is NOT shadowed by the generic /{uid}/{tier} route
        lst = client.get(f"/api/attachments/{att['uid']}/renditions")
        assert lst.status_code == 200 and lst.json()[0]["variant_key"] == "print:glossy"

        # the print downloads (household-visible, download-only); a missing variant → 404
        dl = client.get(f"/api/attachments/{att['uid']}/rendition/print:glossy")
        assert dl.status_code == 200
        assert "private" in dl.headers["cache-control"]
        assert client.get(f"/api/attachments/{att['uid']}/rendition/print:matte").status_code == 404

        # the single-attachment meta carries the same shape
        meta = client.get(f"/api/attachments/{att['uid']}").json()
        assert meta["hasPrint"] and not meta["hasLegacyPrint"] and len(meta["renditions"]) == 1

        # ...and so does the library list endpoint (one item — the screen, with its rendition)
        lib = client.get("/api/attachments").json()
        row = next(i for i in lib if i["uid"] == att["uid"])
        assert row["hasPrint"] and not row["hasLegacyPrint"]
        assert [r["variant_key"] for r in row["renditions"]] == ["print:glossy"]
    finally:
        _cleanup()


def test_legacy_print_is_distinguished_from_renditions(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        screen = _put(client, "B.png", PNG + b"b" * 3000)
        memo = client.post("/api/memos", json={"content": "x", "upload_uids": [screen]}).json()
        uid = memo["attachments"][0]["uid"]
        # attach a LEGACY single print master (the old endpoint, unchanged)
        up = client.post(
            f"/api/attachments/{uid}/print",
            files={"file": ("B-print.png", PNG + b"p" * 2000, "image/png")},
        )
        assert up.status_code == 200
        meta = client.get(f"/api/attachments/{uid}").json()
        assert meta["hasPrint"] is True
        assert meta["hasLegacyPrint"] is True  # legacy print → the /print button DOES apply
        assert meta["renditions"] == []  # ...and is NOT a rendition (coexistence)
    finally:
        _cleanup()


def test_rendition_endpoints_require_auth(client):
    # unauthenticated → 401 (the cross-user 404 boundary is _viewable, shared with the print tier)
    assert client.get("/api/attachments/whatever/renditions").status_code == 401
    assert client.get("/api/attachments/whatever/rendition/print:glossy").status_code == 401
