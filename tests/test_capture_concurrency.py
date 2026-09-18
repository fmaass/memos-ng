"""Two writers at once: the memo's lock protocol, and the finalize/completion race (MEMNG-262).

These are REAL concurrency tests — two database sessions on the same event loop, each running the
production code path — not two calls one after the other. Both bugs they pin are invisible to a
sequential test: the position collision needs two edits that both read an empty memo, and the
capture race needs a finalize that starts while a completing append still holds its transaction.
"""

import asyncio
import json
from pathlib import Path

from app import auth
from app.config import settings
from app.csrf import canonical_origin
from app.database import async_session
from app.main import app
from app.models import Upload, User
from app.routers import memos as memos_router
from app.services import uploads as uploads_svc
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

ROOT = Path(__file__).resolve().parent.parent
BINARIES = ROOT / "fixtures" / "auto-memo-date"
ARTEFACT = json.loads((ROOT / "fixtures" / "auto-memo-date.json").read_text())


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachment_renditions"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
        c.execute(text("DELETE FROM memos"))
    eng.dispose()


def _png(marker: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + marker + b"0" * 3000


def _upload(client, name: str, data: bytes, mime: str = "image/png") -> str:
    created = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert created.status_code == 201, created.text
    uid = created.json()["upload_uid"]
    appended = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert appended.status_code == 200, appended.text
    return uid


def _async_client(client) -> AsyncClient:
    """An async client speaking with the SAME session as the TestClient fixture.

    The login flow leaves more than one cookie of that name in the jar (the callback re-sets it),
    so the LAST one — the live session — is the one to carry over.
    """
    sid = [c.value for c in client.cookies.jar if c.name == auth.SID_COOKIE][-1]
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": canonical_origin()},
        cookies={auth.SID_COOKIE: sid or ""},
    )


def _positions(memo_uid: str) -> list[tuple[int | None, str]]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.position, a.filename FROM attachments a JOIN memos m ON m.id = a.memo_id "
                "WHERE m.uid = :u ORDER BY a.position NULLS LAST, a.id"
            ),
            {"u": memo_uid},
        ).all()
    eng.dispose()
    return [(p, f) for p, f in rows]


async def test_two_concurrent_appends_to_one_memo_do_not_both_allocate_position_zero(
    logged_in_client, monkeypatch, tmp_path
):
    """Both edits read the memo's attachments to decide the next position. Without the memo row
    lock, two that arrive together both read an EMPTY memo and both write 0 — the memo then has
    two attachments claiming the same place and its order is decided by id after all."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = client.post(
            "/api/memos", json={"content": "empty to start", "visibility": "PRIVATE"}
        ).json()
        first_uid = _upload(client, "first.png", _png(b"1"))
        second_uid = _upload(client, "second.png", _png(b"2"))

        entered = asyncio.Event()
        release = asyncio.Event()
        real_link = memos_router.svc.link_attachments
        calls = 0

        async def park_after_allocating(*args, **kwargs):
            """Hold the FIRST edit inside its transaction AFTER it has allocated a position and
            BEFORE it commits — the only window in which a second edit can read the memo as still
            empty. That window is the bug: whatever the second edit does here is what the lock
            protocol decides. (With the memo lock in place the second edit never reaches this
            function while the first one is parked, which is the point.)"""
            nonlocal calls
            calls += 1
            result = await real_link(*args, **kwargs)
            if calls == 1:
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(memos_router.svc, "link_attachments", park_after_allocating)

        async with _async_client(client) as first, _async_client(client) as second:
            a = asyncio.create_task(
                first.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [first_uid]})
            )
            await asyncio.wait_for(entered.wait(), timeout=10)
            b = asyncio.create_task(
                second.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [second_uid]})
            )
            await asyncio.sleep(0.4)  # long enough for the second to reach the memo's lock
            release.set()
            first_res, second_res = await asyncio.gather(a, b)

        assert first_res.status_code == 200, first_res.text
        assert second_res.status_code == 200, second_res.text
        saved = _positions(memo["uid"])
        assert [p for p, _ in saved] == [0, 1], saved  # two places, not one place twice
        assert sorted(name for _, name in saved) == ["first.png", "second.png"]
    finally:
        _cleanup()


async def test_finalize_waits_for_a_completing_append_to_land_its_capture_read(
    logged_in_client, monkeypatch, tmp_path
):
    """The append that COMPLETES an upload writes the capture read in its own transaction. A
    finalize that starts meanwhile sees the bytes complete on disk — the concat lands before the
    commit — so without taking the row lock it copies the capture columns as they were BEFORE the
    read and stamps an attachment with no date the file plainly has."""
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = (BINARIES / "jpeg-offset.jpg").read_bytes()
        created = client.post(
            "/api/uploads",
            json={"filename": "jpeg-offset.jpg", "mime": "image/jpeg", "size": len(data)},
        )
        uid = created.json()["upload_uid"]
        client.patch(f"/api/uploads/{uid}", content=data[:200], headers={"Upload-Offset": "0"})

        async def rest():
            yield data[200:]

        async def completing_append():
            async with async_session() as session:
                upload = (
                    await session.execute(select(Upload).where(Upload.upload_uid == uid))
                ).scalar_one()
                await uploads_svc.append_stream(session, upload, 200, rest())
                await asyncio.sleep(0.4)  # the window a racing finalize would slip through
                await session.commit()

        async def racing_finalize():
            await asyncio.sleep(0.1)  # start after the append has the row, before it commits
            async with async_session() as session:
                owner = (
                    await session.execute(select(User).where(User.subject == subject))
                ).scalar_one()
                upload = (
                    await session.execute(select(Upload).where(Upload.upload_uid == uid))
                ).scalar_one()
                attachment = await uploads_svc.finalize(session, upload, owner.id)
                await session.commit()
                return attachment.uid

        _, attachment_uid = await asyncio.gather(completing_append(), racing_finalize())

        eng = create_engine(SYNC_URL)
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT capture_instant, capture_source, captured_at FROM attachments "
                    "WHERE uid = :u"
                ),
                {"u": attachment_uid},
            ).one()
        eng.dispose()
        expected = ARTEFACT["binaries"]["jpeg-offset.jpg"]["expect"]
        assert row.capture_instant is not None, "the finalize raced past the capture read"
        assert row.capture_instant.strftime("%Y-%m-%dT%H:%M:%SZ") == expected["capture_instant"]
        assert row.capture_source == expected["capture_source"]
        assert row.captured_at == row.capture_instant
    finally:
        _cleanup()


async def test_the_second_patch_applies_to_what_the_row_says_after_the_first(
    logged_in_client, monkeypatch, tmp_path
):
    """A silent lost update, and the reason the memo lock also RE-READS.

    Both edits load the memo before either commits, so the second one holds the winner's
    before-picture. Setting a field back to the value in that stale snapshot then changes nothing
    at all — SQLAlchemy compares against the snapshot, finds them equal and emits no UPDATE — and
    the edit reports success while the first edit's value survives.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        memo = client.post("/api/memos", json={"content": "orig", "visibility": "PRIVATE"}).json()

        entered = asyncio.Event()
        release = asyncio.Event()
        real_lock = memos_router.svc.lock_memo_for_write
        calls = 0

        async def park_holding_the_lock(*args, **kwargs):
            nonlocal calls
            calls += 1
            locked = await real_lock(*args, **kwargs)
            if calls == 1:
                entered.set()
                await release.wait()  # the second edit is queued on this row meanwhile
            return locked

        monkeypatch.setattr(memos_router.svc, "lock_memo_for_write", park_holding_the_lock)

        async with _async_client(client) as first, _async_client(client) as second:
            a = asyncio.create_task(
                first.patch(f"/api/memos/{memo['uid']}", json={"content": "changed"})
            )
            await asyncio.wait_for(entered.wait(), timeout=10)
            b = asyncio.create_task(
                second.patch(f"/api/memos/{memo['uid']}", json={"content": "orig"})
            )
            await asyncio.sleep(0.3)  # long enough for the second to load and queue for the lock
            release.set()
            first_res, second_res = await asyncio.gather(a, b)

        assert [first_res.status_code, second_res.status_code] == [200, 200]
        stored = client.get(f"/api/memos/{memo['uid']}").json()
        assert stored["content"] == "orig"  # the LAST writer wins, and it actually wrote
        assert second_res.json()["content"] == "orig"  # …and said so
    finally:
        _cleanup()


async def test_a_finalize_that_waited_for_a_consumed_upload_answers_unknown(
    logged_in_client, monkeypatch, tmp_path
):
    """Waiting on the upload's row lock is exactly when the row can disappear: the writer ahead may
    be another finalize, which consumes the session and deletes it. The loser must take the
    unknown-upload path — not carry on and touch a part file that now belongs to the winner (or to
    nobody), which is a FileNotFoundError at best and someone else's bytes at worst."""
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _upload(client, "shared.png", _png(b"ONCE"))

        async def winner():
            async with async_session() as session:
                owner = (
                    await session.execute(select(User).where(User.subject == subject))
                ).scalar_one()
                upload = (
                    await session.execute(select(Upload).where(Upload.upload_uid == uid))
                ).scalar_one()
                attachment = await uploads_svc.finalize(session, upload, owner.id)
                await asyncio.sleep(0.4)  # the loser queues on the row meanwhile
                await session.commit()
                return attachment.uid

        async def loser():
            await asyncio.sleep(0.1)
            async with async_session() as session:
                owner = (
                    await session.execute(select(User).where(User.subject == subject))
                ).scalar_one()
                upload = (
                    await session.execute(select(Upload).where(Upload.upload_uid == uid))
                ).scalar_one()
                try:
                    await uploads_svc.finalize(session, upload, owner.id)
                except uploads_svc.UploadsUnlinkable as exc:
                    await session.rollback()
                    return exc
                await session.commit()
                return None

        winner_uid, refused = await asyncio.gather(winner(), loser())

        assert isinstance(refused, uploads_svc.UploadsUnlinkable), refused
        assert refused.missing == [uid]
        eng = create_engine(SYNC_URL)
        with eng.connect() as conn:
            uids = list(conn.execute(text("SELECT uid FROM attachments")).scalars())
        eng.dispose()
        assert uids == [winner_uid]  # one attachment, one owner of those bytes
    finally:
        _cleanup()
