"""Withdrawing an upload session: `DELETE /api/uploads/{upload_uid}` (MEMNG-953).

The app withdraws a session when its person removes a pending card whose bytes already reached the
server, so nothing it no longer wants can be finalized later. Every case drives the real route or
the real service against the real database, and reads the outcome back from the `uploads` and
`attachments` tables and from the part file on disk.
"""

import asyncio

import pytest
from app.config import settings
from app.database import async_session
from app.main import app
from app.models import Upload, User
from app.services import uploads as svc
from prometheus_client import REGISTRY
from sqlalchemy import select, text

from tests.test_attachments_taken import _sql
from tests.test_passes_api import _wait_until_blocked_by
from tests.test_uploads import _cleanup, _drop_user, _login_as, _reap_expired_committed, _start

WITHDRAWN = "upload_withdrawn"
INTRUDER = "withdraw-intruder"
RACER = "withdraw-racer"


@pytest.fixture
def data_root(db_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    yield tmp_path
    _cleanup()
    _drop_user(INTRUDER)
    _drop_user(RACER)


def _complete(client, data: bytes = b"withdraw-me" * 100, name: str = "w.bin") -> str:
    uid = _start(client, name, "application/octet-stream", len(data))
    r = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert r.status_code == 200, r.text
    return uid


def _row(uid: str):
    rows = _sql(
        "SELECT withdrawn_at, expires_at, bytes_received FROM uploads WHERE upload_uid = :u",
        {"u": uid},
    )
    return rows[0] if rows else None


def _attachment_uids() -> set[str]:
    return {r.uid for r in _sql("SELECT uid FROM attachments")}


def _sample(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def _expired_tally() -> int:
    rows = _sql("SELECT count FROM upload_session_tallies WHERE result = 'expired'")
    return int(rows[0].count) if rows else 0


# --- the route ----------------------------------------------------------------------------------


def test_withdraw_answers_204_and_a_finalize_after_it_409s(logged_in_client, data_root):
    client, _ = logged_in_client
    uid = _complete(client)
    r = client.delete(f"/api/uploads/{uid}")
    assert r.status_code == 204, r.text
    assert r.content == b""
    assert _row(uid).withdrawn_at is not None
    fin = client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 409, fin.text
    assert fin.json()["detail"]["code"] == WITHDRAWN
    assert fin.json()["detail"]["message"]
    assert _attachment_uids() == set()
    # The refusal consumed nothing: the bytes wait for the reap, not for a second finalize.
    assert svc.part_path(uid).exists()


def test_withdraw_twice_answers_204_both_times_and_stamps_once(logged_in_client, data_root):
    client, _ = logged_in_client
    uid = _complete(client)
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    first = _row(uid)
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    assert _row(uid) == first


def test_withdraw_of_an_unknown_uid_answers_204(logged_in_client, data_root):
    client, _ = logged_in_client
    assert client.delete("/api/uploads/no-such-upload").status_code == 204


def test_withdraw_of_a_foreign_session_answers_204_and_the_session_survives(
    logged_in_client, monkeypatch, data_root
):
    victim, _ = logged_in_client
    uid = _complete(victim)
    before = _row(uid)
    intruder = _login_as(monkeypatch, INTRUDER)
    assert intruder.delete(f"/api/uploads/{uid}").status_code == 204
    assert _row(uid) == before
    assert before.withdrawn_at is None
    fin = victim.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200, fin.text


def test_a_patch_after_withdraw_is_refused_and_lands_no_bytes(logged_in_client, data_root):
    client, _ = logged_in_client
    uid = _start(client, "p.bin", "application/octet-stream", 20)
    first = client.patch(f"/api/uploads/{uid}", content=b"a" * 10, headers={"Upload-Offset": "0"})
    assert first.status_code == 200
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    more = client.patch(f"/api/uploads/{uid}", content=b"b" * 10, headers={"Upload-Offset": "10"})
    assert more.status_code == 409, more.text
    assert more.json()["detail"] == WITHDRAWN
    assert _row(uid).bytes_received == 10
    assert svc.part_path(uid).stat().st_size == 10


def test_a_withdrawn_session_is_not_advertised_as_resumable(logged_in_client, data_root):
    client, _ = logged_in_client
    uid = _start(client, "h.bin", "application/octet-stream", 20)
    assert client.head(f"/api/uploads/{uid}").status_code == 200
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    head = client.head(f"/api/uploads/{uid}")
    assert head.status_code == 404
    assert "Upload-Offset" not in head.headers
    assert client.get(f"/api/uploads/{uid}/capture").status_code == 404


def test_the_reap_removes_a_withdrawn_session_and_its_part_and_spares_an_open_one(
    logged_in_client, data_root
):
    client, _ = logged_in_client
    gone = _complete(client)
    kept = _complete(client, b"keep-me" * 100, "k.bin")
    assert client.delete(f"/api/uploads/{gone}").status_code == 204
    assert svc.part_path(gone).exists() and svc.part_path(kept).exists()
    # The hourly reap as the worker runs it: no `now` of its own, the clock's.
    assert _reap_expired_committed() == 1
    assert _row(gone) is None
    assert not svc.part_path(gone).exists()
    assert _row(kept) is not None
    assert svc.part_path(kept).exists()
    # Reaped, it is an unknown uid like any other: withdrawing it again changes nothing.
    assert client.delete(f"/api/uploads/{gone}").status_code == 204
    assert client.post(f"/api/uploads/{gone}/finalize").status_code == 404


def test_withdrawing_a_finalized_upload_answers_204_and_the_attachment_stands(
    logged_in_client, data_root
):
    client, _ = logged_in_client
    uid = _complete(client)
    fin = client.post(f"/api/uploads/{uid}/finalize")
    assert fin.status_code == 200, fin.text
    attachment = fin.json()["uid"]
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    assert _attachment_uids() == {attachment}
    assert client.get(f"/api/attachments/{attachment}").status_code == 200


def test_a_memo_naming_a_withdrawn_upload_is_refused_as_missing(logged_in_client, data_root):
    client, _ = logged_in_client
    uid = _complete(client)
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    posted = client.post(
        "/api/memos",
        json={"content": "withdrawn", "visibility": "PUBLIC", "upload_uids": [uid]},
    )
    assert posted.status_code == 409, posted.text
    assert posted.json()["detail"] == {
        "code": "uploads_unlinkable",
        "missing_upload_uids": [uid],
        "incomplete_upload_uids": [],
    }
    assert _attachment_uids() == set()


def test_a_withdrawal_is_counted_once_as_withdrawn_and_never_as_expired(
    logged_in_client, data_root
):
    client, _ = logged_in_client
    withdrawn = _sample("memosng_upload_sessions_total", {"result": "withdrawn"})
    expired = _expired_tally()
    uid = _complete(client)
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    assert client.delete(f"/api/uploads/{uid}").status_code == 204
    assert client.delete("/api/uploads/no-such-upload").status_code == 204
    assert _sample("memosng_upload_sessions_total", {"result": "withdrawn"}) - withdrawn == 1
    assert _reap_expired_committed() == 1
    assert _expired_tally() == expired


def test_the_schema_declares_the_withdraw_and_the_finalize_refusal():
    paths = app.openapi()["paths"]
    delete = paths["/api/uploads/{upload_uid}"]["delete"]
    assert set(delete["responses"]) >= {"204"}
    assert "content" not in delete["responses"]["204"]
    conflict = paths["/api/uploads/{upload_uid}/finalize"]["post"]["responses"]["409"]
    ref = conflict["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/UploadWithdrawnError")


# --- races: two transactions, one row lock ------------------------------------------------------


async def _committed_upload(data: bytes, *, append: bool = True) -> tuple[int, str]:
    async with async_session() as s:
        owner = User(subject=RACER, role="user")
        s.add(owner)
        await s.flush()
        upload = await svc.create_upload(
            s,
            owner_id=owner.id,
            filename="r.bin",
            mime="application/octet-stream",
            size=len(data),
        )
        if append:
            assert await svc.append_chunk(s, upload, 0, data) is not None
        await s.commit()
        return owner.id, upload.upload_uid


async def _backend_pid(s) -> int:
    return (await s.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def test_a_withdraw_queued_behind_a_finalize_that_commits_changes_nothing(data_root):
    owner_id, uid = await _committed_upload(b"race-finalize-first" * 50)
    async with async_session() as fin_s, async_session() as wd_s:
        upload = (await fin_s.execute(select(Upload).where(Upload.upload_uid == uid))).scalar_one()
        # finalize's own first step: the row lock, held until its commit.
        held = await svc.ensure_capture(fin_s, upload)
        assert held is not None
        withdraw = asyncio.create_task(svc.withdraw(wd_s, uid, owner_id))
        await _wait_until_blocked_by(await _backend_pid(fin_s))
        attachment = await svc.finalize(fin_s, held, owner_id=owner_id)
        await fin_s.commit()
        assert await withdraw is False
        await wd_s.commit()
    assert _attachment_uids() == {attachment.uid}
    assert _row(uid) is None


async def test_a_finalize_queued_behind_a_withdraw_that_commits_is_refused(data_root):
    owner_id, uid = await _committed_upload(b"race-withdraw-first" * 50)
    async with async_session() as wd_s, async_session() as fin_s:
        # Read before the withdraw commits: the snapshot finalize starts from is a live session.
        stale = (await fin_s.execute(select(Upload).where(Upload.upload_uid == uid))).scalar_one()
        assert await svc.withdraw(wd_s, uid, owner_id) is True
        finalize = asyncio.create_task(svc.finalize(fin_s, stale, owner_id=owner_id))
        await _wait_until_blocked_by(await _backend_pid(wd_s))
        await wd_s.commit()
        with pytest.raises(svc.UploadWithdrawn):
            await finalize
        await fin_s.rollback()
    assert _attachment_uids() == set()
    assert _row(uid).withdrawn_at is not None
    assert svc.part_path(uid).exists()


async def test_an_append_whose_body_streams_across_a_withdraw_lands_no_bytes(data_root):
    owner_id, uid = await _committed_upload(b"z" * 20, append=False)

    async def body():
        yield b"x" * 10
        # The client is still sending when the same person withdraws the session elsewhere.
        async with async_session() as w:
            assert await svc.withdraw(w, uid, owner_id) is True
            await w.commit()

    async with async_session() as s:
        upload = (await s.execute(select(Upload).where(Upload.upload_uid == uid))).scalar_one()
        with pytest.raises(svc.UploadWithdrawn):
            await svc.append_stream(s, upload, 0, body())
        await s.rollback()
    assert _row(uid).bytes_received == 0
    assert svc.part_path(uid).stat().st_size == 0
    assert list((data_root / "uploads").glob("*.spool")) == []
