"""MEMNG-959 — skip the upload when the server already holds the bytes.

`POST /api/uploads` takes an optional `sha256`. When the caller already owns an attachment with
those bytes (same digest, same size, original on disk), the session is created COMPLETE — its part
file a hard link to that original — and finalizing it mints a NEW attachment that reuses the
twin's derived tiers and blurhash with no second derive. Every other case is the ordinary upload.

The last half pins the delete side: two memos holding the same bytes, deleted in BOTH orders.
The tiers and the original each survive the first delete and are gone after the second, for both
sharing shapes there are — the hard-linked original this feature creates (safe by construction:
each row has its own name) and two rows NAMING one path (what `reclaim`'s reference check
protects; the mutation `tests/mutations/no_reclaim_path_refcount.patch` removes that check).
"""

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import pytest
from app.config import settings
from app.database import async_session
from app.services import reclaim
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_uploads import _drop_user, _login_as

PNG = b"\x89PNG\r\n\x1a\n"
ONE = PNG + b"one-photo" * 400
# Same length as ONE, different bytes: an edited representation of "the same" picture.
EDITED = PNG + b"one-phot0" * 400
LONGER = ONE + b"x"
TIERS = ("thumbs", "mobile")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _exec(sql: str, **params) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text(sql), params)
    eng.dispose()


def _att(uid: str) -> dict:
    rows = _rows(
        "SELECT id, storage_path, sha256, size, status, blurhash, width, height, mime "
        "FROM attachments WHERE uid = :u",
        u=uid,
    )
    assert len(rows) == 1, f"{uid} is not a single attachment row: {rows}"
    keys = ("id", "storage_path", "sha256", "size", "status", "blurhash", "width", "height", "mime")
    return dict(zip(keys, rows[0], strict=True))


def _jobs_for(attachment_id: int) -> int:
    return _rows("SELECT count(*) FROM media_jobs WHERE attachment_id = :i", i=attachment_id)[0][0]


def _tier(uid: str, tier: str) -> Path:
    return Path(settings.DATA_ROOT) / "assets" / tier / f"{uid}.jpg"


def _start(client, data: bytes, *, sha256: str | None = None, name: str = "p.png") -> dict:
    body: dict = {"filename": name, "mime": "image/png", "size": len(data)}
    if sha256 is not None:
        body["sha256"] = sha256
    opened = client.post("/api/uploads", json=body)
    assert opened.status_code == 201, opened.text
    return opened.json()


def _upload(client, data: bytes, *, sha256: str | None = None) -> str:
    """The ordinary path: open, then send every byte."""
    session = _start(client, data, sha256=sha256)
    assert session["bytes_received"] == 0, session
    uid = session["upload_uid"]
    sent = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    return uid


def _memo(client, upload_uid: str, content: str) -> dict:
    made = client.post(
        "/api/memos",
        json={"content": content, "visibility": "PRIVATE", "upload_uids": [upload_uid]},
    )
    assert made.status_code == 201, made.text
    return made.json()


def _derive(uid: str) -> None:
    """Stand in for the worker's fast lane, which tests do not run: publish the two image tiers
    (distinct bytes per uid, so a copy is recognisable) and mark the row ready with its dims and
    blurhash — exactly the columns `media.process_job` writes."""
    for tier in TIERS:
        path = _tier(uid, tier)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{tier}-of-{uid}".encode())
    _exec(
        "UPDATE attachments SET status = 'ready', width = 640, height = 480, "
        "blurhash = 'LEHV6nWB2yk8pyo0adR*.7kCMdnj' WHERE uid = :u",
        u=uid,
    )


def _ready_memo(client, data: bytes = ONE, content: str = "may") -> tuple[dict, str]:
    memo = _memo(client, _upload(client, data), content)
    uid = memo["attachments"][0]["uid"]
    _derive(uid)
    return memo, uid


# ---- staging --------------------------------------------------------------------------------


def test_a_staging_hit_answers_a_complete_session_backed_by_the_original(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])

        session = _start(client, ONE, sha256=_sha(ONE))

        assert session["bytes_received"] == len(ONE)  # complete: no PATCH needed
        uid = session["upload_uid"]
        part = tmp_path / "uploads" / f"{uid}.part"
        assert os.path.samefile(part, original)  # a hard link, not a copy
        head = client.head(f"/api/uploads/{uid}")
        assert head.headers["Upload-Offset"] == str(len(ONE))
        row = _rows(
            "SELECT bytes_received, declared_size FROM uploads WHERE upload_uid = :u", u=uid
        )
        assert row == [(len(ONE), len(ONE))]
    finally:
        _cleanup()


@pytest.mark.parametrize("finalize_by", ["route", "memo"])
def test_finalizing_a_staging_hit_reuses_the_tiers_with_no_new_job(
    logged_in_client, monkeypatch, tmp_path, finalize_by
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        before = _att(twin)
        jobs_before = _rows("SELECT count(*) FROM media_jobs")[0][0]
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]

        if finalize_by == "route":
            done = client.post(f"/api/uploads/{staged}/finalize")
            assert done.status_code == 200, done.text
            new_uid = done.json()["uid"]
            assert done.json()["status"] == "ready"
        else:
            new_uid = _memo(client, staged, "june")["attachments"][0]["uid"]

        assert new_uid != twin  # a NEW row for the new memo, the twin stays the old memo's
        new = _att(new_uid)
        assert new["sha256"] == before["sha256"] == _sha(ONE)
        assert new["size"] == len(ONE)
        assert new["status"] == "ready"
        assert (new["blurhash"], new["width"], new["height"]) == (
            before["blurhash"],
            before["width"],
            before["height"],
        )
        assert new["mime"] == before["mime"]
        assert _jobs_for(new["id"]) == 0  # no second derive
        assert _rows("SELECT count(*) FROM media_jobs")[0][0] == jobs_before
        # Its own original NAME on the same bytes, and its own COPY of every tier.
        assert new["storage_path"] != before["storage_path"]
        assert os.path.samefile(new["storage_path"], before["storage_path"])
        for tier in TIERS:
            assert _tier(new_uid, tier).read_bytes() == _tier(twin, tier).read_bytes()
            assert not os.path.samefile(_tier(new_uid, tier), _tier(twin, tier))
        assert client.get(f"/api/attachments/{new_uid}").status_code == 200
        assert _rows("SELECT count(*) FROM uploads WHERE upload_uid = :u", u=staged) == [(0,)]
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("sent", "claimed"),
    [
        pytest.param(EDITED, EDITED, id="same-size-different-bytes"),
        pytest.param(LONGER, LONGER, id="different-size-different-bytes"),
        pytest.param(LONGER, ONE, id="matching-hash-but-a-different-size"),
    ],
)
def test_a_hash_mismatch_is_an_ordinary_upload(
    logged_in_client, monkeypatch, tmp_path, sent, claimed
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _ready_memo(client)
        uid = _upload(client, sent, sha256=_sha(claimed))  # asserts bytes_received == 0
        done = client.post(f"/api/uploads/{uid}/finalize")
        assert done.status_code == 200, done.text
        new = _att(done.json()["uid"])
        assert new["sha256"] == _sha(sent)
        assert new["status"] == "processing"
        assert _jobs_for(new["id"]) == 1  # derived as any upload is
    finally:
        _cleanup()


def test_another_owners_attachment_is_never_a_candidate(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    other_subject = "staging_other_owner"
    try:
        other = _login_as(monkeypatch, other_subject)
        _, theirs = _ready_memo(other)
        their_original = Path(_att(theirs)["storage_path"])

        session = _start(client, ONE, sha256=_sha(ONE))

        assert session["bytes_received"] == 0
        part = tmp_path / "uploads" / f"{session['upload_uid']}.part"
        assert part.stat().st_size == 0
        assert not os.path.samefile(part, their_original)
    finally:
        _cleanup()
        _drop_user(other_subject)


def test_no_sha256_behaves_exactly_as_before(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _ready_memo(client)
        assert _start(client, ONE)["bytes_received"] == 0
    finally:
        _cleanup()


@pytest.mark.parametrize(
    "bad",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", " " + "a" * 63],
    ids=["uppercase", "short", "long", "not-hex", "empty", "padded"],
)
def test_a_malformed_sha256_is_refused(logged_in_client, monkeypatch, tmp_path, bad):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    r = client.post(
        "/api/uploads",
        json={"filename": "p.png", "mime": "image/png", "size": len(ONE), "sha256": bad},
    )
    assert r.status_code == 422, r.text


def test_a_missing_original_falls_back_to_the_ordinary_upload(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        Path(_att(twin)["storage_path"]).unlink()
        _upload(client, ONE, sha256=_sha(ONE))  # asserts bytes_received == 0
    finally:
        _cleanup()


def test_a_link_failure_falls_back_to_the_ordinary_upload(logged_in_client, monkeypatch, tmp_path):
    """EXDEV — an original on another filesystem than the upload parts — and EPERM."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _ready_memo(client)

        def refuse(src, dst, **kwargs):
            raise OSError(18, "Invalid cross-device link", str(src))

        monkeypatch.setattr(os, "link", refuse)
        uid = _upload(client, ONE, sha256=_sha(ONE))
        assert client.post(f"/api/uploads/{uid}/finalize").status_code == 200
    finally:
        _cleanup()


def test_a_complete_session_never_writes_into_the_shared_original(
    logged_in_client, monkeypatch, tmp_path
):
    """The part file IS the original's inode, so a stray PATCH must never reach it: a replay at
    offset 0 is a no-op and a body past the declared size is refused before it lands."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        uid = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]

        replay = client.patch(
            f"/api/uploads/{uid}", content=b"X" * 9, headers={"Upload-Offset": "0"}
        )
        past = client.patch(
            f"/api/uploads/{uid}", content=b"X", headers={"Upload-Offset": str(len(ONE))}
        )

        assert replay.status_code in (200, 204, 409), replay.text
        assert past.status_code == 413, past.text
        assert original.read_bytes() == ONE
    finally:
        _cleanup()


def test_a_twin_deleted_before_finalize_still_finalizes_from_the_staged_bytes(
    logged_in_client, monkeypatch, tmp_path
):
    """The hard link holds the bytes: the twin's memo deleted between staging and finalize leaves
    the staged session finalizing as an ordinary upload, derived afresh."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo, twin = _ready_memo(client)
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        assert client.delete(f"/api/memos/{memo['uid']}").status_code == 204
        assert _rows("SELECT count(*) FROM attachments WHERE uid = :u", u=twin) == [(0,)]

        done = client.post(f"/api/uploads/{staged}/finalize")

        assert done.status_code == 200, done.text
        new = _att(done.json()["uid"])
        assert Path(new["storage_path"]).read_bytes() == ONE
        assert new["status"] == "processing"
        assert _jobs_for(new["id"]) == 1
    finally:
        _cleanup()


def test_a_finalize_that_fails_after_a_staging_hit_walks_everything_back_off_the_loop(
    logged_in_client, monkeypatch, tmp_path
):
    """A finalize whose write does not land takes back the tier copies it made under the new uid
    and puts the staged part file back — and does it in a worker thread, not on the event loop."""
    from app.routers import uploads as uploads_router
    from app.services import storage

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []
        on_the_loop: list[bool] = []
        real_finalize = uploads_router.svc.finalize
        real_copy_restore = storage.TrackedCopy.restore

        async def finalize_then_fail(*args, **kwargs):
            attachment = await real_finalize(*args, **kwargs)
            minted.append(attachment.uid)
            raise RuntimeError("the commit never happens")

        def recording_restore(self):
            # The TestClient runs the app's loop in a thread of its own, so "not the main thread"
            # proves nothing: ask whether THIS thread is running an event loop.
            try:
                asyncio.get_running_loop()
                on_the_loop.append(True)
            except RuntimeError:
                on_the_loop.append(False)
            return real_copy_restore(self)

        monkeypatch.setattr(uploads_router.svc, "finalize", finalize_then_fail)
        monkeypatch.setattr(storage.TrackedCopy, "restore", recording_restore)

        failed = client.post(f"/api/uploads/{staged}/finalize")

        assert failed.status_code == 500
        assert len(minted) == 1
        assert [_tier(minted[0], tier).exists() for tier in TIERS] == [False, False]
        assert os.path.samefile(part, original)  # the staged bytes are back where they were
        assert original.read_bytes() == ONE
        assert on_the_loop == [False] * len(TIERS)
    finally:
        _cleanup()


# ---- delete order ---------------------------------------------------------------------------


def _staged_pair(client) -> tuple[dict, dict]:
    """Two memos on one set of bytes, the second one created by a staging hit."""
    first, _ = _ready_memo(client, content="may")
    staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
    assert _rows("SELECT bytes_received FROM uploads WHERE upload_uid = :u", u=staged) == [
        (len(ONE),)
    ]
    second = _memo(client, staged, "june")
    return first, second


def _same_path_pair(client) -> tuple[dict, dict]:
    """Two memos whose rows NAME one original path — the shape the reference check protects. No
    write path mints it today; it is what a row pointed at another's file would look like."""
    first, a = _ready_memo(client, content="may")
    second, b = _ready_memo(client, content="june")
    shared = _att(a)["storage_path"]
    own = Path(_att(b)["storage_path"])
    _exec("UPDATE attachments SET storage_path = :p WHERE uid = :u", p=shared, u=b)
    own.unlink()
    return first, second


def _files(uid: str, storage_path: str) -> list[Path]:
    return [Path(storage_path)] + [_tier(uid, tier) for tier in TIERS]


@pytest.mark.parametrize("order", ["first-then-second", "second-then-first"])
@pytest.mark.parametrize("shape", ["staged", "same-path"])
def test_two_memos_sharing_an_original_delete_in_either_order(
    logged_in_client, monkeypatch, tmp_path, shape, order
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        pair = _staged_pair(client) if shape == "staged" else _same_path_pair(client)
        doomed, survivor = pair if order == "first-then-second" else pair[::-1]
        doomed_uid = doomed["attachments"][0]["uid"]
        survivor_uid = survivor["attachments"][0]["uid"]
        survivor_files = _files(survivor_uid, _att(survivor_uid)["storage_path"])
        doomed_files = _files(doomed_uid, _att(doomed_uid)["storage_path"])
        survivor_bytes = [p.read_bytes() for p in survivor_files]
        assert survivor_bytes[0] == ONE

        assert client.delete(f"/api/memos/{doomed['uid']}").status_code == 204

        assert _rows("SELECT count(*) FROM attachments WHERE uid = :u", u=doomed_uid) == [(0,)]
        assert [p.read_bytes() for p in survivor_files] == survivor_bytes  # all still there
        gone = [p for p in doomed_files if p not in survivor_files]
        assert [p for p in gone if p.exists()] == []
        assert client.get(f"/api/memos/{survivor['uid']}").status_code == 200

        assert client.delete(f"/api/memos/{survivor['uid']}").status_code == 204

        assert _rows("SELECT count(*) FROM attachments WHERE sha256 = :s", s=_sha(ONE)) == [(0,)]
        assert [p for p in survivor_files + doomed_files if p.exists()] == []
    finally:
        _cleanup()


async def test_two_concurrent_reclaims_of_a_shared_path_never_leak_it(
    logged_in_client, monkeypatch, tmp_path
):
    """Each reclaim sees the OTHER row still there unless the two are serialised on the path: both
    would keep the file, and both rows would go — bytes with no row. The first reclaim is held in
    its unlink step (row lock and path lock taken) while the second starts."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        first, second = await asyncio.to_thread(_same_path_pair, client)
        a = _att(first["attachments"][0]["uid"])
        b = _att(second["attachments"][0]["uid"])
        shared = Path(a["storage_path"])
        assert b["storage_path"] == a["storage_path"]
        _exec("UPDATE attachments SET memo_id = NULL WHERE id IN (:a, :b)", a=a["id"], b=b["id"])

        entered, release = threading.Event(), threading.Event()

        def held_unlink(paths: list[Path]) -> list[str]:
            entered.set()
            assert release.wait(10)
            return reclaim.unlink_paths(paths)

        async def run(att_id: int, unlink=None) -> reclaim.ReclaimResult:
            async with async_session() as db:
                return await reclaim.reclaim_attachment(
                    db, att_id, reclaim.Revalidate.IDENTITY, unlink=unlink
                )

        first_task = asyncio.create_task(run(a["id"], held_unlink))
        assert await asyncio.to_thread(entered.wait, 10)
        second_task = asyncio.create_task(run(b["id"]))
        await asyncio.sleep(0.5)  # long enough for an unserialised second reclaim to finish
        release.set()
        results = await asyncio.gather(first_task, second_task)

        assert [r.outcome for r in results] == ["reclaimed", "reclaimed"]
        assert not shared.exists()
    finally:
        _cleanup()
