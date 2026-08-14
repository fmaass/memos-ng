"""The interactive print-master verbs (MEMNG-75): attach/replace, remove, and measured dimensions.

These exercise the endpoints the web UI drives — the resumable-upload → `attach_rendition` path
that writes an `attachment_renditions` row. The legacy multipart `/print` route (print_path) is a
different pipe with its own tests and is deliberately untouched here (MEMNG-51 retires it).
"""

import io
from pathlib import Path

from app.config import settings
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM attachment_renditions"))
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
        c.execute(text("DELETE FROM memos"))
        c.execute(text("DELETE FROM users WHERE subject='print_other_owner'"))
    eng.dispose()


def _png(width: int, height: int, colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, "PNG")
    return buf.getvalue()


def _upload(client, name: str, data: bytes, mime: str = "image/png", declared: int | None = None):
    """Push bytes through the resumable upload endpoints and return the upload_uid."""
    r = client.post(
        "/api/uploads",
        json={
            "filename": name,
            "mime": mime,
            "size": declared if declared is not None else len(data),
            "last_modified": 1609459200000,
        },
    )
    assert r.status_code == 201
    uid = r.json()["upload_uid"]
    client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    return uid


def _memo_with_photo(client, name="_AR9 - screen.png", data=None):
    """A memo carrying one screen photo → (memo, attachment)."""
    screen = _upload(client, name, data if data is not None else _png(6, 4))
    memo = client.post("/api/memos", json={"content": "shot", "upload_uids": [screen]}).json()
    return memo, memo["attachments"][0]


def _attach(client, att_uid: str, upload_uid: str, variant_key: str | None = None):
    body: dict = {"upload_uid": upload_uid}
    if variant_key is not None:
        body["variant_key"] = variant_key
    return client.post(f"/api/attachments/{att_uid}/renditions", json=body)


def _rendition_rows(att_uid: str) -> list[tuple[str, str | None, int, str]]:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        rows = c.execute(
            text(
                "SELECT r.variant_key, r.label, r.size, r.storage_path "
                "FROM attachment_renditions r JOIN attachments a ON a.id = r.attachment_id "
                "WHERE a.uid = :u ORDER BY r.id"
            ),
            {"u": att_uid},
        ).all()
    eng.dispose()
    return [(k, lbl, size, path) for k, lbl, size, path in rows]


def _seed_other_owner_attachment(uid: str, mime: str = "image/png"):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject='print_other_owner'"))
        oid = c.execute(
            text("INSERT INTO users (subject, role) VALUES ('print_other_owner','user') "
                 "RETURNING id")
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, filename, mime, size, status, "
                "storage_path) VALUES (:u,:o,'x.png',:m,1,CAST('ready' AS attachment_status),'/x')"
            ),
            {"u": uid, "o": oid, "m": mime},
        )
    eng.dispose()


def test_attach_writes_a_rendition_row_and_serves_it(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        master = _png(31, 17, (200, 100, 50))
        up = _upload(client, "_AR9 - print.png", master)

        r = _attach(client, att["uid"], up)
        assert r.status_code == 201
        meta = r.json()
        # The response is the photo's fresh state: the UI renders the print row straight from it.
        assert meta["hasPrint"] is True
        assert meta["hasLegacyPrint"] is False  # the legacy print_path column stays untouched
        assert [x["variant_key"] for x in meta["renditions"]] == ["print"]
        assert meta["renditions"][0]["size"] == len(master)

        # The row really exists (not just a response shape) and names a file that holds the bytes.
        rows = _rendition_rows(att["uid"])
        assert len(rows) == 1 and rows[0][0] == "print" and rows[0][1] is None
        assert Path(rows[0][3]).read_bytes() == master
        # ...filed beside the screen master in the memo folder, under its own name.
        assert Path(rows[0][3]).name == "_AR9 - print.png"

        # The dimension-bearing route measures the real file.
        detail = client.get(f"/api/attachments/{att['uid']}/renditions")
        assert detail.status_code == 200
        assert (detail.json()[0]["width"], detail.json()[0]["height"]) == (31, 17)

        # ...and it downloads.
        dl = client.get(f"/api/attachments/{att['uid']}/rendition/print")
        assert dl.status_code == 200 and dl.content == master

        # The upload session was consumed — it cannot be replayed onto another photo.
        assert client.head(f"/api/uploads/{up}").status_code == 404
    finally:
        _cleanup()


def test_replace_keeps_one_row_and_one_file(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        first = _png(10, 10, (1, 2, 3))
        assert _attach(client, att["uid"], _upload(client, "master.png", first)).status_code == 201
        original_path = _rendition_rows(att["uid"])[0][3]

        # Re-uploading under the SAME name lands on the same file (no ` (2)` sibling, no orphan).
        second = _png(40, 40, (9, 9, 9))
        assert _attach(client, att["uid"], _upload(client, "master.png", second)).status_code == 201
        rows = _rendition_rows(att["uid"])
        assert len(rows) == 1  # replaced, not accumulated
        assert rows[0][2] == len(second) and rows[0][3] == original_path
        assert Path(original_path).read_bytes() == second

        # A replacement under a DIFFERENT name moves the master and reaps the file it displaced.
        third = _png(5, 5, (7, 7, 7))
        assert _attach(client, att["uid"], _upload(client, "other.png", third)).status_code == 201
        rows = _rendition_rows(att["uid"])
        assert len(rows) == 1 and Path(rows[0][3]).name == "other.png"
        assert Path(rows[0][3]).read_bytes() == third
        assert not Path(original_path).exists()  # displaced master reaped, not leaked
        assert client.get(f"/api/attachments/{att['uid']}/rendition/print").content == third
    finally:
        _cleanup()


def test_labelled_variant_key_is_stored_with_its_slug(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        up = _upload(client, "glossy.png", _png(8, 8))
        r = _attach(client, att["uid"], up, variant_key="print:glossy")
        assert r.status_code == 201
        # Live production masters all carry `print:glossy` — replacing one must keep its identity,
        # or the photo-book selector would suddenly derive from a different row.
        assert _rendition_rows(att["uid"])[0][:2] == ("print:glossy", "glossy")
        dl = client.get(f"/api/attachments/{att['uid']}/rendition/print:glossy")
        assert dl.status_code == 200
    finally:
        _cleanup()


def test_rejects_keys_outside_the_print_variant_grammar(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        for key in ("web", "printable", "print:", "print:Glossy Matte", "print:../etc"):
            up = _upload(client, "x.png", _png(4, 4))
            r = _attach(client, att["uid"], up, variant_key=key)
            assert r.status_code == 400, key
        assert _rendition_rows(att["uid"]) == []  # nothing was written by any of them
    finally:
        _cleanup()


def test_only_the_owner_may_attach_or_remove(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _seed_other_owner_attachment("print-foreign")
        up = _upload(client, "x.png", _png(4, 4))
        assert _attach(client, "print-foreign", up).status_code == 404
        assert _rendition_rows("print-foreign") == []
        # ...and removal is owner-only too: seed a master on the foreign photo, then try to drop it.
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO attachment_renditions "
                    "(attachment_id, variant_key, label, filename, mime, size, storage_path) "
                    "SELECT id,'print',NULL,'f.png','image/png',3,'/tmp/f.png' FROM attachments "
                    "WHERE uid='print-foreign'"
                )
            )
        eng.dispose()
        assert client.delete("/api/attachments/print-foreign/rendition/print").status_code == 404
        assert len(_rendition_rows("print-foreign")) == 1  # survived
    finally:
        _cleanup()


def test_print_masters_are_images_only(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        video = _upload(client, "clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"v" * 200, "video/mp4")
        memo = client.post("/api/memos", json={"content": "clip", "upload_uids": [video]}).json()
        att = memo["attachments"][0]
        up = _upload(client, "master.png", _png(4, 4))
        r = _attach(client, att["uid"], up)
        assert r.status_code == 400 and "images" in r.json()["detail"]
        assert _rendition_rows(att["uid"]) == []
    finally:
        _cleanup()


def test_an_incomplete_upload_never_becomes_a_master(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        data = _png(9, 9)
        partial = _upload(client, "half.png", data, declared=len(data) + 500)
        r = _attach(client, att["uid"], partial)
        assert r.status_code == 400
        assert _rendition_rows(att["uid"]) == []
        meta = client.get(f"/api/attachments/{att['uid']}").json()
        assert meta["hasPrint"] is False
    finally:
        _cleanup()


def test_remove_drops_the_row_and_the_file(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        assert _attach(
            client, att["uid"], _upload(client, "m.png", _png(12, 12))
        ).status_code == 201
        path = Path(_rendition_rows(att["uid"])[0][3])

        r = client.delete(f"/api/attachments/{att['uid']}/rendition/print")
        assert r.status_code == 204
        assert _rendition_rows(att["uid"]) == []
        assert not path.exists()  # the archive master is really gone, not just unlinked from a row
        meta = client.get(f"/api/attachments/{att['uid']}").json()
        assert meta["hasPrint"] is False and meta["renditions"] == []
        # the screen photo is untouched
        assert client.get(f"/api/attachments/{att['uid']}/original").status_code == 200

        assert client.delete(f"/api/attachments/{att['uid']}/rendition/print").status_code == 404
    finally:
        _cleanup()


def test_print_master_write_verbs_require_auth(client):
    attach = client.post("/api/attachments/whatever/renditions", json={"upload_uid": "u"})
    assert attach.status_code == 401
    assert client.delete("/api/attachments/whatever/rendition/print").status_code == 401
