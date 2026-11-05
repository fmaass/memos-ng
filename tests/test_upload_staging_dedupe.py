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
import time
from pathlib import Path

import pytest
from app.config import settings
from app.database import async_session
from app.services import reclaim
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

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
        # The positive control (MEMNG-1167): the same bytes and digest from their OWNER are a hit,
        # so the miss above is the owner scope speaking, not a twin that could never match.
        theirs_again = _start(other, ONE, sha256=_sha(ONE))
        assert theirs_again["bytes_received"] == len(ONE)
        own_part = tmp_path / "uploads" / f"{theirs_again['upload_uid']}.part"
        assert os.path.samefile(own_part, their_original)
    finally:
        _cleanup()
        _drop_user(other_subject)


def test_no_sha256_behaves_exactly_as_before(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _ready_memo(client)
        assert _start(client, ONE)["bytes_received"] == 0
        # The positive control (MEMNG-1167): the same request WITH the digest is a hit, so the miss
        # above is the absent sha256 speaking, not a twin that could never match.
        assert _start(client, ONE, sha256=_sha(ONE))["bytes_received"] == len(ONE)
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


@pytest.mark.parametrize("landed", [True, False], ids=["commit-landed", "commit-did-not-land"])
def test_a_finalize_whose_commit_raises_walks_back_only_when_it_did_not_land(
    logged_in_client, monkeypatch, tmp_path, landed
):
    """MEMNG-1167 — `db.commit()` raising is not proof the transaction rolled back: the connection
    can drop, or the request be cancelled, after PostgreSQL committed. Walking back then would
    move the original out from under a LIVE row and delete the tiers it serves. The route asks the
    database what happened, and only a write that provably did not land is walked back."""
    from app.routers import uploads as uploads_router

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []
        real_finalize = uploads_router.svc.finalize

        async def finalize_then_lose_the_commit_answer(db, *args, **kwargs):
            attachment = await real_finalize(db, *args, **kwargs)
            minted.append(attachment.uid)
            real_commit = db.commit

            async def commit_then_drop_the_connection():
                if landed:
                    await real_commit()
                else:
                    await db.rollback()
                raise ConnectionResetError("the connection dropped around COMMIT")

            db.commit = commit_then_drop_the_connection
            return attachment

        monkeypatch.setattr(uploads_router.svc, "finalize", finalize_then_lose_the_commit_answer)

        failed = client.post(f"/api/uploads/{staged}/finalize")

        assert failed.status_code == 500
        assert len(minted) == 1
        rows = _rows("SELECT storage_path FROM attachments WHERE uid = :u", u=minted[0])
        session_rows = _rows("SELECT count(*) FROM uploads WHERE upload_uid = :u", u=staged)
        if landed:
            # The row is live, so everything it names stays exactly where finalize put it.
            assert len(rows) == 1
            assert Path(rows[0][0]).read_bytes() == ONE
            assert os.path.samefile(rows[0][0], original)
            assert [_tier(minted[0], tier).exists() for tier in TIERS] == [True, True]
            assert not part.exists()
            assert session_rows == [(0,)]
        else:
            assert rows == []
            assert [_tier(minted[0], tier).exists() for tier in TIERS] == [False, False]
            assert os.path.samefile(part, original)  # the staged bytes are back where they were
            assert session_rows == [(1,)]
    finally:
        _cleanup()


def test_a_commit_whose_outcome_cannot_be_learned_leaves_the_files_in_place(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1167 — "unknown" is not "rolled back": when the database cannot answer whether the
    commit landed, the walk-back does not run (a file in the wrong place is recoverable, one taken
    from a live row is not), and the request still fails rather than answering success."""
    from app.routers import uploads as uploads_router
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []
        real_finalize = uploads_router.svc.finalize

        async def finalize_then_lose_the_commit(db, *args, **kwargs):
            attachment = await real_finalize(db, *args, **kwargs)
            minted.append(attachment.uid)

            async def lost():
                await db.rollback()
                raise ConnectionResetError("the connection dropped around COMMIT")

            db.commit = lost
            return attachment

        def no_database():
            raise ConnectionRefusedError("the database is unreachable")

        monkeypatch.setattr(uploads_router.svc, "finalize", finalize_then_lose_the_commit)
        monkeypatch.setattr(uploads_svc, "async_session", no_database)

        failed = client.post(f"/api/uploads/{staged}/finalize")

        assert failed.status_code == 500
        assert len(minted) == 1
        # Not walked back: the tier copies and the placed original are where finalize put them.
        assert [_tier(minted[0], tier).exists() for tier in TIERS] == [True, True]
        assert not part.exists()
        placed = list((tmp_path / "originals" / "_unfiled").iterdir())
        assert len(placed) == 1 and os.path.samefile(placed[0], original)
    finally:
        _cleanup()


async def test_the_commit_outcome_waits_for_the_interrupted_transaction_to_end(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1167 — a commit cut short can still be in flight on the server when the route asks.
    Read without waiting, the session row is still visible (the delete is uncommitted) and the
    answer would be "rolled back" for a write that is about to land; the outcome check takes the
    row's lock, so it answers only once that transaction has ended — here, committed."""
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, existing = await asyncio.to_thread(_ready_memo, client)
        session = await asyncio.to_thread(_start, client, LONGER)
        uid = session["upload_uid"]
        eng = create_engine(SYNC_URL)
        conn = eng.connect()
        try:
            tx = conn.begin()
            conn.execute(text("SELECT 1 FROM uploads WHERE upload_uid = :u FOR UPDATE"), {"u": uid})
            conn.execute(text("DELETE FROM uploads WHERE upload_uid = :u"), {"u": uid})

            asked = asyncio.create_task(uploads_svc.finalize_committed(uid, existing))
            await asyncio.sleep(0.5)
            assert not asked.done()  # waiting on the in-flight transaction, not reading past it
            await asyncio.to_thread(tx.commit)

            assert await asyncio.wait_for(asked, 10) is True
        finally:
            conn.close()
            eng.dispose()
    finally:
        _cleanup()


def _finalize_whose_commit_rolls_back(
    uploads_router, minted: list[str], *, first_close_hangs_s: float = 0
):
    """A `svc.finalize` stand-in that runs the real one, then makes the request's COMMIT roll back
    and raise — the shape a dropped connection gives. `first_close_hangs_s` makes the request
    session's FIRST close (the first thing the outcome resolution does) hang that long; a later one
    (the request's own teardown) closes it for real."""
    real_finalize = uploads_router.svc.finalize

    async def finalize(db, *args, **kwargs):
        attachment = await real_finalize(db, *args, **kwargs)
        minted.append(attachment.uid)

        async def lost():
            await db.rollback()
            raise ConnectionResetError("the connection dropped around COMMIT")

        db.commit = lost
        if first_close_hangs_s:
            real_close = db.close
            closes: list[None] = []

            async def close():
                closes.append(None)
                if len(closes) == 1:
                    await asyncio.sleep(first_close_hangs_s)
                else:
                    await real_close()

            db.close = close
        return attachment

    return finalize


def test_an_expiry_reap_cannot_slip_between_the_rollback_answer_and_the_restore(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1190 — a finalize whose COMMIT raised and provably rolled back puts the staged part
    file back. If the resolver let go of the session row's lock before that restore, an expiry
    reap could take the row in the gap — deleting it, and unlinking a part that is not there yet —
    and the restore would then recreate a part no row will ever collect. The reap is started
    inside the restore's window and given time to finish; it must instead wait for the lock, so
    the row and its part go together."""
    from app.routers import uploads as uploads_router
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        _exec(
            "UPDATE uploads SET expires_at = now() - interval '1 hour' WHERE upload_uid = :u",
            u=staged,
        )
        minted: list[str] = []
        reaped: list[int] = []
        reap_finished_in_the_gap: list[bool] = []
        real_restore = uploads_router._restore_moves_sync

        def reap() -> None:
            reaped.append(asyncio.run(uploads_svc.reap_expired_committed()))

        def restore_with_a_reap_racing_it(moves):
            reaper = threading.Thread(target=reap)
            reaper.start()
            reaper.join(1.5)  # an unfenced reap finishes well inside this
            reap_finished_in_the_gap.append(not reaper.is_alive())
            real_restore(moves)
            restore_with_a_reap_racing_it.reaper = reaper

        monkeypatch.setattr(
            uploads_router.svc,
            "finalize",
            _finalize_whose_commit_rolls_back(uploads_router, minted),
        )
        monkeypatch.setattr(uploads_router, "_restore_moves_sync", restore_with_a_reap_racing_it)

        failed = client.post(f"/api/uploads/{staged}/finalize")
        restore_with_a_reap_racing_it.reaper.join(10)

        assert failed.status_code == 500
        assert len(minted) == 1
        rows = _rows("SELECT count(*) FROM uploads WHERE upload_uid = :u", u=staged)
        # The invariant: a part exists exactly while a session row would collect it.
        assert (rows, part.exists()) == ([(0,)], False)
        assert reaped == [1]  # the reap ran, and took the session part and all…
        assert reap_finished_in_the_gap == [False]  # …once it had waited on the row's lock
        assert Path(_att(twin)["storage_path"]).read_bytes() == ONE
    finally:
        _cleanup()


def test_a_commit_outcome_whose_close_hangs_gives_up_at_the_bound_and_leaves_the_files(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1190 — the outcome resolution has ONE deadline, the request session's close inside
    it. A close that hangs (the database gone) must not hold the request past the bound, and the
    outcome is then unknown: the files stay where finalize put them, although this commit in fact
    rolled back — unknown is not rolled back."""
    from app.routers import uploads as uploads_router
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_TIMEOUT_S", 0.5)
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []
        monkeypatch.setattr(
            uploads_router.svc,
            "finalize",
            _finalize_whose_commit_rolls_back(uploads_router, minted, first_close_hangs_s=6),
        )

        started = time.monotonic()
        failed = client.post(f"/api/uploads/{staged}/finalize")
        elapsed = time.monotonic() - started

        assert failed.status_code == 500
        assert elapsed < 4, elapsed  # the bound, not the hanging close
        assert len(minted) == 1
        assert [_tier(minted[0], tier).exists() for tier in TIERS] == [True, True]
        assert not part.exists()
        placed = list((tmp_path / "originals" / "_unfiled").iterdir())
        assert len(placed) == 1 and os.path.samefile(placed[0], original)
    finally:
        _cleanup()


class _StubHolder:
    """The request session as the outcome resolution sees it: only its close is called."""

    def __init__(self, close_s: float) -> None:
        self.close_s = close_s

    async def close(self) -> None:
        await asyncio.sleep(self.close_s)


@pytest.mark.parametrize("during", ["close", "resolution"])
async def test_a_cancelled_commit_outcome_resolution_still_ends_at_the_bound(
    logged_in_client, monkeypatch, tmp_path, during
):
    """MEMNG-1190 — the request is cancelled while the outcome is being learned: during the
    request session's close (which hangs), or during the resolution itself (the session row's lock
    held elsewhere). The cancellation must not cut the resolution short — it is handed back, for
    the route to re-raise, only once the resolution is over — and the resolution must not outlast
    its one deadline either: before, a hanging close had no bound at all and the lock wait had its
    own 10 s one."""
    from app.routers import uploads as uploads_router
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_TIMEOUT_S", 0.5)
    # The server's lock_timeout ends this far inside the deadline: small, so the resolution still
    # visibly outlives the cancellation at 0.2 s.
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_LOCK_MARGIN_S", 0.05)
    try:
        session = await asyncio.to_thread(_start, client, LONGER)
        uid = session["upload_uid"]
        eng = create_engine(SYNC_URL)
        conn = eng.connect()
        try:
            tx = conn.begin()
            if during == "resolution":
                conn.execute(
                    text("SELECT 1 FROM uploads WHERE upload_uid = :u FOR UPDATE"), {"u": uid}
                )
            holder = _StubHolder(close_s=6 if during == "close" else 0)

            started = time.monotonic()
            asked = asyncio.create_task(uploads_router._commit_outcome(holder, uid, "no-such-uid"))
            await asyncio.sleep(0.2)
            assert not asked.done()
            asked.cancel()
            landed, cancelled = await asyncio.wait_for(asked, 15)
            elapsed = time.monotonic() - started

            assert elapsed < 3, elapsed  # over at the bound
            assert elapsed >= 0.4, elapsed  # …and not cut short by the cancellation at 0.2 s
            assert landed is None  # unknown: the answer never arrived inside the bound
            assert isinstance(cancelled, asyncio.CancelledError)  # handed back to re-raise
            await asyncio.to_thread(tx.rollback)
        finally:
            await asyncio.to_thread(conn.close)
            await asyncio.to_thread(eng.dispose)
    finally:
        # Off the loop: the abandoned resolution's connection is still being closed ON this loop,
        # and its server backend can be holding the row lock until it is — a TRUNCATE run on the
        # loop's own thread would wait on a close that can never run.
        await asyncio.to_thread(_cleanup)


def _row_lock_free(upload_uid: str) -> bool:
    """Whether the session row's lock can be taken right now (NOWAIT): the read-back that the
    resolver's transaction has really ended on the server, not only on the client."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as conn:
            try:
                conn.execute(
                    text("SELECT 1 FROM uploads WHERE upload_uid = :u FOR UPDATE NOWAIT"),
                    {"u": upload_uid},
                )
                return True
            except Exception:
                return False
            finally:
                conn.rollback()
    finally:
        eng.dispose()


async def _eventually_free(upload_uid: str, within_s: float = 2.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_row_lock_free, upload_uid):
            return True
        await asyncio.sleep(0.05)
    return False


def _end_abandoned_transactions() -> None:
    """Test teardown only: end any transaction an abandoned session left open in this database, so
    the cleanup's TRUNCATE is not queued behind it for ever when the code under test leaks one."""
    _exec(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND state LIKE 'idle in transaction%'"
    )


def _resolver_sessions_stalling(monkeypatch, uploads_svc, method: str, stall_s: float) -> None:
    """Make the RESOLVER's own session (the one `finalize_committed` opens) stall in `method` —
    its invalidate or its close — while every other session behaves."""
    real_factory = uploads_svc.async_session

    def factory():
        session = real_factory()

        async def stall(*_args, **_kwargs):
            await asyncio.sleep(stall_s)

        setattr(session, method, stall)
        return session

    monkeypatch.setattr(uploads_svc, "async_session", factory)


async def test_a_stalled_resolver_close_after_a_restore_is_cut_at_the_bound(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1190 r2 — the resolver's own teardown is bounded too. A definite rollback runs the
    restore under the session row's lock, and that restore is NOT cut short, however long it
    takes (the lock is held throughout it); the close that ends the transaction afterwards is what
    is bounded. A close that stalls is abandoned at the bound and its connection dropped, so the
    lock is released on the server, not left to a client that never finishes closing."""
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_TIMEOUT_S", 0.5)
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_LOCK_MARGIN_S", 0.1, raising=False)
    try:
        uid = (await asyncio.to_thread(_start, client, LONGER))["upload_uid"]
        _resolver_sessions_stalling(monkeypatch, uploads_svc, "close", 6)
        held_during_restore: list[bool] = []
        restored: list[bool] = []

        async def slow_restore() -> None:
            await asyncio.sleep(0.8)  # past the resolution's deadline: still not cut short
            held_during_restore.append(not await asyncio.to_thread(_row_lock_free, uid))
            restored.append(True)

        started = time.monotonic()
        answer = await asyncio.wait_for(
            uploads_svc.finalize_committed(uid, "no-such-uid", on_rolled_back=slow_restore), 15
        )
        elapsed = time.monotonic() - started

        assert answer is False
        assert restored == [True] and held_during_restore == [True]
        assert elapsed < 3, elapsed  # the restore plus a bounded teardown, not the stalled close
        assert await _eventually_free(uid)  # …and the transaction really ended on the server
    finally:
        await asyncio.to_thread(_end_abandoned_transactions)
        await asyncio.to_thread(_cleanup)


async def test_a_stalled_resolver_invalidate_after_the_deadline_is_cut_at_the_bound(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1190 r2 — past the resolution's deadline the resolver drops its connection
    (invalidate). That drop is bounded as well: an invalidate that stalls is abandoned at the bound
    and the connection terminated, rather than holding the request indefinitely."""
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_TIMEOUT_S", 0.5)
    monkeypatch.setattr(uploads_svc, "COMMIT_OUTCOME_LOCK_MARGIN_S", 0.1, raising=False)
    try:
        uid = (await asyncio.to_thread(_start, client, LONGER))["upload_uid"]
        eng = create_engine(SYNC_URL)
        conn = eng.connect()
        try:
            tx = conn.begin()
            conn.execute(text("SELECT 1 FROM uploads WHERE upload_uid = :u FOR UPDATE"), {"u": uid})
            _resolver_sessions_stalling(monkeypatch, uploads_svc, "invalidate", 6)

            started = time.monotonic()
            answer = await asyncio.wait_for(uploads_svc.finalize_committed(uid, "no-such-uid"), 15)
            elapsed = time.monotonic() - started

            assert answer is None  # the lock never came: unknown
            assert elapsed < 3, elapsed  # the deadline plus a bounded teardown
            await asyncio.to_thread(tx.rollback)
        finally:
            await asyncio.to_thread(conn.close)
            await asyncio.to_thread(eng.dispose)
        assert await _eventually_free(uid)  # nothing of the resolver's is left waiting on it
    finally:
        await asyncio.to_thread(_end_abandoned_transactions)
        await asyncio.to_thread(_cleanup)


def test_a_memo_create_that_fails_after_the_tier_copies_takes_them_back(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1167 — the rollback AFTER the tier copies, on the memo-create path: a staging hit's
    copies are made inside the create's transaction, and a create that then fails must leave no
    tier under the uid it minted and the staged part file where it was."""
    from app.routers import memos as memos_router

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []
        real_finalize_pending = memos_router.uploads_svc.finalize_pending

        async def finalize_then_fail(*args, **kwargs):
            done = await real_finalize_pending(*args, **kwargs)
            minted.extend(done)
            # The copies are on disk at this point: this is the window the walk-back covers.
            assert [_tier(uid, tier).exists() for uid in done for tier in TIERS] == [True, True]
            raise RuntimeError("the create never commits")

        monkeypatch.setattr(memos_router.uploads_svc, "finalize_pending", finalize_then_fail)

        failed = client.post(
            "/api/memos",
            json={"content": "june", "visibility": "PRIVATE", "upload_uids": [staged]},
        )

        assert failed.status_code == 500
        assert len(minted) == 1 and minted[0] != twin
        assert [_tier(minted[0], tier).exists() for tier in TIERS] == [False, False]
        assert os.path.samefile(part, original)
        assert _rows("SELECT count(*) FROM attachments WHERE uid = :u", u=minted[0]) == [(0,)]
        assert _rows("SELECT count(*) FROM uploads WHERE upload_uid = :u", u=staged) == [(1,)]
        # The twin is untouched: its tiers were the copies' source, never moved.
        assert [_tier(twin, tier).exists() for tier in TIERS] == [True, True]
    finally:
        _cleanup()


def _memo_write_whose_commit_raises(memos_router, minted: list[str], *, landed: bool):
    """A `finalize_pending` stand-in for the memo create and edit: the real one, then the
    request's COMMIT either lands or rolls back — and raises either way, as a connection dropped
    around the COMMIT does."""
    real_finalize_pending = memos_router.uploads_svc.finalize_pending

    async def finalize_pending(db, *args, **kwargs):
        done = await real_finalize_pending(db, *args, **kwargs)
        minted.extend(done)
        real_commit = db.commit

        async def commit_then_drop_the_connection():
            if landed:
                await real_commit()
            else:
                await db.rollback()
            raise ConnectionResetError("the connection dropped around COMMIT")

        db.commit = commit_then_drop_the_connection
        return done

    return finalize_pending


@pytest.mark.parametrize("write", ["create", "edit"])
@pytest.mark.parametrize("landed", [True, False], ids=["commit-landed", "commit-did-not-land"])
def test_a_memo_write_whose_commit_raises_walks_back_only_when_it_did_not_land(
    logged_in_client, monkeypatch, tmp_path, landed, write
):
    """MEMNG-1190 — the memo create and edit had the walk-back shape MEMNG-1167 fixed for the
    finalize route: a raise out of `db.commit()` was read as a rollback, so a COMMIT that landed
    before the connection dropped had its original moved back out of the memo folder and its tier
    copies deleted, under a live row. Only a write that provably did not land is walked back."""
    from app.routers import memos as memos_router

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        host, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []
        monkeypatch.setattr(
            memos_router.uploads_svc,
            "finalize_pending",
            _memo_write_whose_commit_raises(memos_router, minted, landed=landed),
        )

        if write == "create":
            failed = client.post(
                "/api/memos",
                json={"content": "june", "visibility": "PRIVATE", "upload_uids": [staged]},
            )
        else:
            failed = client.patch(f"/api/memos/{host['uid']}", json={"upload_uids": [staged]})

        assert failed.status_code == 500
        assert len(minted) == 1 and minted[0] != twin
        rows = _rows("SELECT storage_path FROM attachments WHERE uid = :u", u=minted[0])
        session_rows = _rows("SELECT count(*) FROM uploads WHERE upload_uid = :u", u=staged)
        if landed:
            # The write is stored, so everything its row names stays where the write put it.
            assert len(rows) == 1
            assert Path(rows[0][0]).read_bytes() == ONE
            assert os.path.samefile(rows[0][0], original)
            assert [_tier(minted[0], tier).exists() for tier in TIERS] == [True, True]
            assert not part.exists()
            assert session_rows == [(0,)]
        else:
            assert rows == []
            assert [_tier(minted[0], tier).exists() for tier in TIERS] == [False, False]
            assert os.path.samefile(part, original)  # the staged bytes are back where they were
            assert session_rows == [(1,)]
        assert [_tier(twin, tier).exists() for tier in TIERS] == [True, True]
    finally:
        _cleanup()


@pytest.mark.parametrize("write", ["create", "edit"])
def test_a_memo_write_whose_commit_outcome_cannot_be_learned_leaves_the_files(
    logged_in_client, monkeypatch, tmp_path, write
):
    """MEMNG-1190 — unknown is not rolled back, on the memo writes too: when the database cannot
    say whether the COMMIT landed, nothing is walked back, and the request still fails."""
    from app.routers import memos as memos_router
    from app.services import uploads as uploads_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        host, twin = _ready_memo(client)
        original = Path(_att(twin)["storage_path"])
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        part = tmp_path / "uploads" / f"{staged}.part"
        minted: list[str] = []

        def no_database():
            raise ConnectionRefusedError("the database is unreachable")

        monkeypatch.setattr(
            memos_router.uploads_svc,
            "finalize_pending",
            _memo_write_whose_commit_raises(memos_router, minted, landed=False),
        )
        monkeypatch.setattr(uploads_svc, "async_session", no_database)

        if write == "create":
            failed = client.post(
                "/api/memos",
                json={"content": "june", "visibility": "PRIVATE", "upload_uids": [staged]},
            )
        else:
            failed = client.patch(f"/api/memos/{host['uid']}", json={"upload_uids": [staged]})

        assert failed.status_code == 500
        assert len(minted) == 1
        # Not walked back: the tier copies and the placed original stay where the write put them.
        assert [_tier(minted[0], tier).exists() for tier in TIERS] == [True, True]
        assert not part.exists()
        placed = [p for p in (tmp_path / "originals").rglob("*") if p.is_file() and p != original]
        assert len(placed) == 1 and os.path.samefile(placed[0], original)
    finally:
        _cleanup()


@pytest.mark.parametrize("finalize_by", ["route", "memo"])
def test_a_staging_hit_counts_as_linked_not_completed(
    logged_in_client, monkeypatch, tmp_path, finalize_by
):
    """MEMNG-1167 — a session finalized by link is its own result: no byte crossed the wire and no
    derive ran, so counting it as `completed` hid how often the client's digest saved an upload.
    Both consumers of a staged session count it so: the finalize route and the memo create."""
    from prometheus_client import REGISTRY

    def sessions(result: str) -> float:
        value = REGISTRY.get_sample_value("memosng_upload_sessions_total", {"result": result})
        return 0.0 if value is None else value

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        _ready_memo(client)  # one ordinary upload: counted as completed before the baseline
        staged = _start(client, ONE, sha256=_sha(ONE))["upload_uid"]
        linked, completed = sessions("linked"), sessions("completed")

        if finalize_by == "route":
            done = client.post(f"/api/uploads/{staged}/finalize")
            assert done.status_code == 200, done.text
        else:
            _memo(client, staged, "june")

        assert sessions("linked") - linked == 1
        assert sessions("completed") == completed
        # The control: an ordinary upload still counts as completed, not linked.
        plain = _upload(client, LONGER)
        assert client.post(f"/api/uploads/{plain}/finalize").status_code == 200
        assert sessions("completed") - completed == 1
        assert sessions("linked") - linked == 1
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


def _profile(uid: str) -> Path:
    """A non-default print profile's rendition — enumerated off disk by reclaim, never derived."""
    return Path(settings.DATA_ROOT) / "assets" / "printready" / "matte" / f"{uid}.jpg"


@pytest.mark.parametrize("reclaimer", ["async", "sync"])
@pytest.mark.parametrize(
    "column", ["rendition-names-a-tier", "print-path-names-a-profile"], ids=["rendition", "print"]
)
def test_a_tier_path_another_row_names_survives_reclaim(
    logged_in_client, monkeypatch, tmp_path, column, reclaimer
):
    """MEMNG-1167 — the reference check covers EVERY path reclaim unlinks, not only the ones the
    row names in a column: a derived tier and an enumerated print profile are unlinked too, and
    another row's rendition or print path naming one of them must keep it on disk. Both reclaims —
    the app's (a memo delete) and the maintenance script's synchronous one."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        doomed_memo, doomed = _ready_memo(client, content="may")
        other_memo, other = _ready_memo(client, LONGER, content="june")
        profile = _profile(doomed)
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_bytes(b"matte-of-doomed")
        other_id = _att(other)["id"]
        if column == "rendition-names-a-tier":
            named = _tier(doomed, "thumbs")
            _exec(
                "INSERT INTO attachment_renditions "
                "(attachment_id, variant_key, filename, mime, size, storage_path) "
                "VALUES (:a, 'print', 'p.jpg', 'image/jpeg', 1, :p)",
                a=other_id,
                p=str(named),
            )
        else:
            named = profile
            _exec("UPDATE attachments SET print_path = :p WHERE id = :a", p=str(named), a=other_id)
        kept = named.read_bytes()

        if reclaimer == "async":
            assert client.delete(f"/api/memos/{doomed_memo['uid']}").status_code == 204
        else:
            eng = create_engine(SYNC_URL)
            try:
                with Session(eng) as session:
                    done = reclaim.reclaim_attachment_sync(
                        session, _att(doomed)["id"], reclaim.Revalidate.IDENTITY
                    )
            finally:
                eng.dispose()
            assert done.outcome == "reclaimed"

        assert _rows("SELECT count(*) FROM attachments WHERE uid = :u", u=doomed) == [(0,)]
        assert named.read_bytes() == kept  # the other row's file, still there
        # The control: everything the doomed row alone owned is gone.
        own = [_tier(doomed, tier) for tier in TIERS] + [profile]
        assert [p for p in own if p != named and p.exists()] == []
        # And the last row to name it takes it: nothing is leaked by keeping it.
        assert client.delete(f"/api/memos/{other_memo['uid']}").status_code == 204
        assert not named.exists()
    finally:
        _cleanup()


@pytest.mark.parametrize("reclaimer", ["async", "sync"])
@pytest.mark.parametrize(
    "column",
    ["rendition-names-a-tier", "rendition-names-a-variant", "print-path-names-a-profile"],
    ids=["rendition", "variant", "print"],
)
def test_the_namer_reclaimed_first_leaves_the_tier_its_owner_derives(
    logged_in_client, monkeypatch, tmp_path, column, reclaimer
):
    """MEMNG-1190 — the reverse order of the test above. Row B's rendition or print path names a
    file in row A's derived tier set. A names it through its uid alone, in no column, so a check
    that only reads columns finds no other row naming it and reclaiming B FIRST unlinks A's
    picture. A path a surviving row owns through its uid is that row's, whichever row names it."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        owner_memo, owner = _ready_memo(client, content="may")
        namer_memo, namer = _ready_memo(client, LONGER, content="june")
        profile = _profile(owner)
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_bytes(b"matte-of-owner")
        variant = _tier(owner, "thumbs").with_suffix(".webp")
        variant.write_bytes(b"webp-of-owner")
        namer_id = _att(namer)["id"]
        if column == "print-path-names-a-profile":
            named = profile
            _exec("UPDATE attachments SET print_path = :p WHERE id = :a", p=str(named), a=namer_id)
        else:
            named = _tier(owner, "thumbs") if column == "rendition-names-a-tier" else variant
            _exec(
                "INSERT INTO attachment_renditions "
                "(attachment_id, variant_key, filename, mime, size, storage_path) "
                "VALUES (:a, 'print', 'p.jpg', 'image/jpeg', 1, :p)",
                a=namer_id,
                p=str(named),
            )
        kept = named.read_bytes()

        if reclaimer == "async":
            assert client.delete(f"/api/memos/{namer_memo['uid']}").status_code == 204
        else:
            eng = create_engine(SYNC_URL)
            try:
                with Session(eng) as session:
                    done = reclaim.reclaim_attachment_sync(
                        session, namer_id, reclaim.Revalidate.IDENTITY
                    )
            finally:
                eng.dispose()
            assert done.outcome == "reclaimed"

        assert _rows("SELECT count(*) FROM attachments WHERE uid = :u", u=namer) == [(0,)]
        assert named.read_bytes() == kept  # the surviving owner's file, still there
        # The control: the namer's own uid-derived tiers went with it.
        assert [p for p in (_tier(namer, tier) for tier in TIERS) if p.exists()] == []
        # And the owner's reclaim takes it: nothing is leaked by keeping it.
        assert client.delete(f"/api/memos/{owner_memo['uid']}").status_code == 204
        assert not named.exists()
    finally:
        _cleanup()


def test_a_rows_own_column_naming_its_own_tier_does_not_keep_it(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1167 boundary — the reference check asks about OTHER rows: a row whose own print path
    names one of its own print profiles is the last name of that file, so reclaim takes it."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo, uid = _ready_memo(client)
        profile = _profile(uid)
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_bytes(b"matte")
        _exec("UPDATE attachments SET print_path = :p WHERE uid = :u", p=str(profile), u=uid)

        assert client.delete(f"/api/memos/{memo['uid']}").status_code == 204

        assert _rows("SELECT count(*) FROM attachments WHERE uid = :u", u=uid) == [(0,)]
        assert not profile.exists()
    finally:
        _cleanup()


def test_a_path_that_only_starts_with_a_tier_path_does_not_keep_the_tier(
    logged_in_client, monkeypatch, tmp_path
):
    """MEMNG-1167 literal-in-string — the reference check matches whole paths. An interrupted
    encode's `<uid>.jpg.tmp` sits beside the profile `<uid>.jpg` and both are reclaimed; another
    row naming the `.tmp` keeps that file only, never the profile whose path is its prefix."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        doomed_memo, doomed = _ready_memo(client, content="may")
        _, other = _ready_memo(client, LONGER, content="june")
        profile = _profile(doomed)
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_bytes(b"matte")
        lookalike = profile.with_name(profile.name + ".tmp")
        lookalike.write_bytes(b"named by the other row")
        _exec(
            "INSERT INTO attachment_renditions "
            "(attachment_id, variant_key, filename, mime, size, storage_path) "
            "VALUES (:a, 'print', 'p.jpg', 'image/jpeg', 1, :p)",
            a=_att(other)["id"],
            p=str(lookalike),
        )

        assert client.delete(f"/api/memos/{doomed_memo['uid']}").status_code == 204

        assert not profile.exists()
        assert lookalike.read_bytes() == b"named by the other row"
    finally:
        _cleanup()


def test_an_implicit_owner_is_matched_by_its_whole_uid_never_a_prefix(monkeypatch, tmp_path):
    """MEMNG-1190 literal-in-string — a surviving row owns a tier or profile file through its uid,
    matched as the WHOLE name stem: uid `ab` owns `ab.jpg` and `ab.jpg.tmp` in a profile
    directory, never `abc.jpg`; and a uid with a dot in it is still found as a candidate."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    matte = tmp_path / "assets" / "printready" / "matte"
    thumbs = tmp_path / "assets" / "thumbs"
    named = [
        str(matte / "ab.jpg"),
        str(matte / "ab.jpg.tmp"),
        str(matte / "abc.jpg"),
        str(thumbs / "abc.jpg"),
        str(thumbs / "a.b.jpg"),
        str(tmp_path / "originals" / "ab.jpg"),  # not a derived directory: never implicit
    ]

    assert reclaim._implicitly_owned("ab", named) == {named[0], named[1]}
    assert reclaim._implicitly_owned("abc", named) == {named[2], named[3]}
    assert reclaim._implicitly_owned("a.b", named) == {named[4]}
    candidates = reclaim._implicit_owner_candidates(named)
    assert {"ab", "abc", "a", "a.b"} <= set(candidates)
    assert reclaim._implicitly_owned("ab", [named[5]]) == set()
