"""The interactive print-master verbs (MEMNG-75): attach/replace, remove, and measured dimensions.

These exercise the endpoints the web UI drives — the resumable-upload → `attach_rendition` path
that writes an `attachment_renditions` row. The legacy multipart `/print` route (print_path) is a
different pipe with its own tests and is deliberately untouched here (MEMNG-51 retires it).
"""

import hashlib
import io
from pathlib import Path

from app.config import settings
from app.database import engine as async_engine
from PIL import Image
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

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
            text(
                "INSERT INTO users (subject, role) VALUES ('print_other_owner','user') RETURNING id"
            )
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
        # same AttachmentMeta as the detail route, MEMNG-864 fields included
        assert (meta["filename"], meta["addedBy"]) == ("_AR9 - screen.png", "T")

        # The row really exists (not just a response shape) and names a file that holds the bytes.
        rows = _rendition_rows(att["uid"])
        assert len(rows) == 1 and rows[0][0] == "print" and rows[0][1] is None
        assert Path(rows[0][3]).read_bytes() == master
        # ...filed beside the screen master in the memo folder, under its own name.
        assert Path(rows[0][3]).name == "_AR9 - print.png"

        # The cache-busting revision is the master's own content digest, so it moves iff the bytes
        # do — the download URL itself is `immutable` and cannot carry that news on its own.
        assert meta["renditions"][0]["rev"] == hashlib.sha256(master).hexdigest()[:8]

        # The dimension-bearing route measures the real file.
        detail = client.get(f"/api/attachments/{att['uid']}/renditions")
        assert detail.status_code == 200
        assert (detail.json()[0]["width"], detail.json()[0]["height"]) == (31, 17)
        assert detail.json()[0]["rev"] == meta["renditions"][0]["rev"]

        # ...and it downloads.
        dl = client.get(f"/api/attachments/{att['uid']}/rendition/print")
        assert dl.status_code == 200 and dl.content == master

        # The upload session was consumed — it cannot be replayed onto another photo.
        assert client.head(f"/api/uploads/{up}").status_code == 404
    finally:
        _cleanup()


def test_a_failed_commit_never_destroys_the_master_it_would_have_replaced(
    logged_in_client, monkeypatch, tmp_path
):
    """The data-loss invariant: the only copy of the CURRENT master must survive until the
    transaction that dereferences it commits, and a committed row's metadata must never describe
    bytes that are gone. Driven by making the commit fail with the replacement already on disk."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        first = _png(10, 10, (1, 2, 3))
        assert _attach(client, att["uid"], _upload(client, "master.png", first)).status_code == 201
        committed = _rendition_rows(att["uid"])[0]
        original_path = Path(committed[3])

        # The upload is prepared BEFORE the commit is sabotaged (its own endpoints commit too).
        replacement = _upload(client, "master.png", _png(40, 40, (9, 9, 9)))

        real_commit = AsyncSession.commit

        async def failing_commit(self):
            raise RuntimeError("commit refused")

        monkeypatch.setattr(AsyncSession, "commit", failing_commit)
        assert _attach(client, att["uid"], replacement).status_code == 500  # the app's own handler
        monkeypatch.setattr(AsyncSession, "commit", real_commit)

        # The row is untouched AND still describes the bytes that are actually there.
        assert _rendition_rows(att["uid"]) == [committed]
        assert original_path.exists() and original_path.read_bytes() == first
        assert client.get(f"/api/attachments/{att['uid']}/rendition/print").content == first
        # ...and the replacement's file was reaped rather than left as a stray master.
        strays = [p for p in original_path.parent.iterdir() if p.name.startswith("master")]
        assert strays == [original_path]
    finally:
        _cleanup()


def test_a_failed_flush_reaps_the_file_it_had_already_placed(
    logged_in_client, monkeypatch, tmp_path
):
    """The narrower window inside the service itself: a raise between the O_EXCL placement and
    the return never reaches the handler's rollback-unlink (the handler only learns the path from
    the return value), so the service must reap its own placement before propagating."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        first = _png(10, 10, (1, 2, 3))
        assert _attach(client, att["uid"], _upload(client, "master.png", first)).status_code == 201
        committed = _rendition_rows(att["uid"])[0]
        original_path = Path(committed[3])

        replacement = _upload(client, "master.png", _png(40, 40, (9, 9, 9)))

        real_flush = AsyncSession.flush

        async def failing_flush(self, *args, **kwargs):
            raise RuntimeError("flush refused")

        monkeypatch.setattr(AsyncSession, "flush", failing_flush)
        assert _attach(client, att["uid"], replacement).status_code == 500
        monkeypatch.setattr(AsyncSession, "flush", real_flush)

        assert _rendition_rows(att["uid"]) == [committed]
        assert original_path.exists() and original_path.read_bytes() == first
        strays = [p for p in original_path.parent.iterdir() if p.name.startswith("master")]
        assert strays == [original_path]
    finally:
        _cleanup()


def test_attach_reads_the_rendition_state_under_the_row_lock(
    logged_in_client, monkeypatch, tmp_path
):
    """A STRUCTURAL ordering assertion, deliberately not a concurrency test: the TestClient drives
    one request at a time, so a genuine two-writer interleave cannot be staged here without a fake.
    What is asserted is the property the interleave depends on — the attachment is read with FOR
    UPDATE, and every read of attachment_renditions happens after that lock is taken. Reverting to
    read-then-lock puts the selectin load of the renditions ahead of it and turns this red."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    try:
        _, att = _memo_with_photo(client)
        up = _upload(client, "m.png", _png(6, 6))
        event.listen(async_engine.sync_engine, "before_cursor_execute", record)
        try:
            assert _attach(client, att["uid"], up).status_code == 201
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", record)

        locked = [
            i
            for i, s in enumerate(statements)
            if "FROM attachments" in s and "FOR UPDATE" in s and "attachments.uid" in s
        ]
        renditions = [i for i, s in enumerate(statements) if "FROM attachment_renditions" in s]
        assert locked, f"the attachment is never read FOR UPDATE: {statements}"
        assert renditions, "positive control: the request does read attachment_renditions"
        assert min(renditions) > min(locked)
    finally:
        _cleanup()


def test_replace_keeps_one_row_and_one_file(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, att = _memo_with_photo(client)
        first = _png(10, 10, (1, 2, 3))
        r = _attach(client, att["uid"], _upload(client, "master.png", first))
        assert r.status_code == 201
        first_rev = r.json()["renditions"][0]["rev"]
        original_path = Path(_rendition_rows(att["uid"])[0][3])

        # Even under the SAME client filename the replacement is written as its own file — the
        # current master's bytes are never overwritten in place (see the failed-commit test) — and
        # the file it displaces is reaped once the swap is committed.
        second = _png(40, 40, (9, 9, 9))
        r = _attach(client, att["uid"], _upload(client, "master.png", second))
        assert r.status_code == 201
        rows = _rendition_rows(att["uid"])
        assert len(rows) == 1  # replaced, not accumulated
        assert rows[0][2] == len(second)
        assert Path(rows[0][3]) != original_path and Path(rows[0][3]).read_bytes() == second
        assert not original_path.exists()  # displaced master reaped, not leaked
        # The URL is unchanged, so the cache key has to move with the bytes (FIX 3).
        assert r.json()["renditions"][0]["rev"] != first_rev

        # A replacement under a DIFFERENT name behaves identically.
        previous_path = Path(rows[0][3])
        third = _png(5, 5, (7, 7, 7))
        assert _attach(client, att["uid"], _upload(client, "other.png", third)).status_code == 201
        rows = _rendition_rows(att["uid"])
        assert len(rows) == 1 and Path(rows[0][3]).name == "other.png"
        assert Path(rows[0][3]).read_bytes() == third
        assert not previous_path.exists()
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
        # "" is explicit nonsense, not an absent key — only absence may default to `print`.
        for key in ("", " ", "web", "printable", "print:", "print:Glossy Matte", "print:../etc"):
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
        assert (
            _attach(client, att["uid"], _upload(client, "m.png", _png(12, 12))).status_code == 201
        )
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
