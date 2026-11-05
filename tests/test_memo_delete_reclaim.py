"""MEMNG-170 — deleting a memo reclaims its attachments, rows and files.

Owner report: memos created and deleted from the iOS app were gone everywhere, but their images
were still on the web SPA's resources page. `delete_memo` deleted the memo and nothing else — the
`attachments` relationship carries no cascade and the FK is ON DELETE SET NULL — so every
attachment survived with `memo_id` NULL, its files were never unlinked, and its media jobs were
never swept.

Dedup only ever reuses an UNLINKED attachment and `link_attachments` refuses to re-point one that
belongs elsewhere, so an attachment ROW on a memo is exclusively that memo's. Its FILES need not
be: since MEMNG-959 reclaim keeps any path another row still names (and since MEMNG-1167 that
covers every path it unlinks, derived tiers and print profiles included) — that reference check is
pinned in tests/test_upload_staging_dedupe.py. The tests below pin both halves of the ordinary
case — what the delete takes, and what it must not touch.
"""

from pathlib import Path

from app.config import settings
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"
ONE = PNG + b"one" * 700
TWO = PNG + b"two" * 700


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "TRUNCATE attachment_renditions, media_jobs, attachments, memo_tags, "
                "reactions, memos, uploads RESTART IDENTITY CASCADE"
            )
        )
    eng.dispose()


def _rows(sql: str, **params) -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [tuple(r) for r in c.execute(text(sql), params)]
    eng.dispose()
    return out


def _storage_path(uid: str) -> str:
    rows = _rows("SELECT storage_path FROM attachments WHERE uid = :u", u=uid)
    assert len(rows) == 1, f"{uid} is not a single attachment row: {rows}"
    return rows[0][0]


def _attachment_id(uid: str) -> int:
    return _rows("SELECT id FROM attachments WHERE uid = :u", u=uid)[0][0]


def _surviving(uids: list[str]) -> list[str]:
    """Which of THESE uids still have a row — the shared test database holds other tests' rows,
    so every assertion here is scoped to the media this test made."""
    found = _rows("SELECT uid FROM attachments WHERE uid = ANY(:u)", u=uids)
    return sorted(row[0] for row in found)


def _jobs_for(ids: list[int]) -> list[int]:
    sql = "SELECT id FROM media_jobs WHERE attachment_id = ANY(:i)"
    return [row[0] for row in _rows(sql, i=ids)]


def _upload(client, name: str, data: bytes) -> str:
    opened = client.post(
        "/api/uploads", json={"filename": name, "mime": "image/png", "size": len(data)}
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    return uid


def _memo_with(client, payloads: list[bytes], content="pics") -> dict:
    uids = [_upload(client, f"p{i}.png", body) for i, body in enumerate(payloads)]
    made = client.post(
        "/api/memos", json={"content": content, "visibility": "PUBLIC", "upload_uids": uids}
    )
    assert made.status_code == 201, made.text
    return made.json()


def _orphan(client, data: bytes) -> str:
    """An attachment finalized on its own — no memo, which is what the library lists as `orphan`."""
    uid = _upload(client, "loose.png", data)
    done = client.post(f"/api/uploads/{uid}/finalize")
    assert done.status_code == 200, done.text
    return done.json()["uid"]


def test_deleting_a_memo_takes_its_attachments_rows_files_and_jobs(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with(client, [ONE, TWO])
        uids = [a["uid"] for a in memo["attachments"]]
        assert len(uids) == 2
        originals = [Path(_storage_path(u)) for u in uids]
        assert all(p.exists() for p in originals)
        ids = [_attachment_id(u) for u in uids]
        assert len(_jobs_for(ids)) == 2  # one fast-lane job per attachment

        gone = client.delete(f"/api/memos/{memo['uid']}")
        assert gone.status_code == 204, gone.text

        assert _surviving(uids) == []  # no row left holding memo_id NULL
        assert _jobs_for(ids) == []
        assert [p for p in originals if p.exists()] == []  # …and the bytes are gone with them
        for u in uids:
            assert client.get(f"/api/attachments/{u}").status_code == 404
        listed = {a["uid"] for a in client.get("/api/attachments").json()}
        assert listed.isdisjoint(uids)  # the resources page no longer shows them
    finally:
        _cleanup()


def test_a_deleted_memo_leaves_an_unrelated_orphan_alone(logged_in_client, monkeypatch, tmp_path):
    """The 54 migration-era orphans in production are legitimate media nobody's memo owns. A memo
    delete may not sweep them, even when one holds byte-identical content."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with(client, [ONE])
        # Finalized AFTER the memo, so dedup cannot reuse the memo's (now linked) attachment: this
        # is a second row holding the same bytes, with no memo of its own.
        loose = _orphan(client, ONE)
        loose_file = Path(_storage_path(loose))
        assert loose_file.exists()

        assert client.delete(f"/api/memos/{memo['uid']}").status_code == 204

        assert _surviving([memo["attachments"][0]["uid"], loose]) == [loose]
        assert loose_file.exists()
        assert client.get(f"/api/attachments/{loose}").status_code == 200
    finally:
        _cleanup()


def test_a_second_memo_holding_the_same_bytes_survives(logged_in_client, monkeypatch, tmp_path):
    """The 'still-referenced' case, and why it needs no reference count: posting the same photo to a
    second memo mints its own row and its own file, so deleting the first cannot touch it."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        first = _memo_with(client, [ONE], content="first")
        second = _memo_with(client, [ONE], content="second")
        first_uid = first["attachments"][0]["uid"]
        second_uid = second["attachments"][0]["uid"]
        assert first_uid != second_uid  # separate rows by design, never a shared one
        first_file = Path(_storage_path(first_uid))
        second_file = Path(_storage_path(second_uid))
        assert first_file != second_file

        assert client.delete(f"/api/memos/{first['uid']}").status_code == 204

        assert _surviving([first_uid, second_uid]) == [second_uid]
        assert not first_file.exists()
        assert second_file.exists()
        assert client.get(f"/api/memos/{second['uid']}").status_code == 200
    finally:
        _cleanup()


def test_reposting_the_same_bytes_after_a_delete_never_resurrects_the_old_row(
    logged_in_client, monkeypatch, tmp_path
):
    """The hazard the reclaim creates if the row were merely unlinked: dedup would hand the next
    upload of these bytes an attachment whose file this delete had already removed."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = _memo_with(client, [ONE])
        old_uid = memo["attachments"][0]["uid"]
        assert client.delete(f"/api/memos/{memo['uid']}").status_code == 204

        again = _memo_with(client, [ONE], content="posted again")
        new_uid = again["attachments"][0]["uid"]
        assert new_uid != old_uid  # a fresh row: there was nothing left to dedup onto
        assert Path(_storage_path(new_uid)).read_bytes() == ONE
        assert client.get(f"/api/attachments/{old_uid}").status_code == 404
    finally:
        _cleanup()
