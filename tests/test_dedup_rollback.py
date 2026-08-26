"""MEMNG-142 — a dedup hit must not destroy the bytes a rolled-back upload row still promises.

`finalize` can answer with an attachment that ALREADY holds the uploaded bytes (same owner, same
sha256, not yet linked to a memo). It used to unlink the part file and delete the Upload row on
the spot — but only the row deletion is transactional. A later failure in the same create (another
upload's finalize, the memo commit itself) rolls the row back to pending with its bytes already
gone, so the retry the client is entitled to fails `uploads_incomplete` on a row the API reports as
consumable, and the only copy was on the client.
"""

import hashlib

from app.config import settings
from app.models import User
from app.services import uploads as up_svc
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"
TWIN = PNG + b"twin" * 500  # the bytes that exist twice
OTHER = PNG + b"other" * 500


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


def _upload(client, name, data, mime="image/png") -> str:
    opened = client.post("/api/uploads", json={"filename": name, "mime": mime, "size": len(data)})
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert sent.status_code == 200, sent.text
    return uid


def _an_unlinked_twin(client, data: bytes) -> str:
    """Finalize an upload on its own: an attachment holding these bytes and NO memo — exactly what
    `finalize`'s dedup looks for (`memo_id IS NULL`, same owner, same sha256)."""
    uid = _upload(client, "twin.png", data)
    done = client.post(f"/api/uploads/{uid}/finalize")
    assert done.status_code == 200, done.text
    return done.json()["uid"]


def test_a_failed_mixed_create_leaves_the_deduped_upload_consumable(
    logged_in_client, monkeypatch, tmp_path
):
    """The acceptance case: one dedup-matched upload + one ordinary one, and a later step fails.

    Both restored Upload rows must still name their bytes — the deduped one because nothing may
    unlink it before the commit, the ordinary one because `_restore_moved` walks it back — and the
    proof is that the IDENTICAL create then succeeds.
    """
    from app.services import memos as memo_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    async def boom(*args, **kwargs):
        raise RuntimeError("the create failed after the uploads were finalized")

    try:
        twin_att = _an_unlinked_twin(client, TWIN)
        dedup_uid = _upload(client, "again.png", TWIN)  # identical bytes → dedup hit
        plain_uid = _upload(client, "other.png", OTHER)  # a genuine move, in the same batch

        monkeypatch.setattr(memo_svc, "create_memo", boom)
        failed = client.post(
            "/api/memos",
            json={
                "content": "doomed",
                "visibility": "PUBLIC",
                "upload_uids": [dedup_uid, plain_uid],
            },
        )
        assert failed.status_code == 500

        # THE regression: the deduped upload's bytes are still there, byte for byte...
        assert up_svc.part_path(dedup_uid).read_bytes() == TWIN
        # ...and so are the ordinary one's, which the restore walked back.
        assert up_svc.part_path(plain_uid).read_bytes() == OTHER

        monkeypatch.undo()
        monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
        retried = client.post(
            "/api/memos",
            json={
                "content": "retried",
                "visibility": "PUBLIC",
                "upload_uids": [dedup_uid, plain_uid],
            },
        )
        assert retried.status_code == 201, retried.text
        uids = {a["uid"] for a in retried.json()["attachments"]}
        assert len(uids) == 2
        assert twin_att in uids  # the dedup really did hand back the pre-existing attachment
        # Consumed for real this time: the commit landed, so the parts are reaped.
        assert not up_svc.part_path(dedup_uid).exists()
        assert not up_svc.part_path(plain_uid).exists()
    finally:
        _cleanup()


def test_a_dedup_on_the_one_shot_finalize_route_reaps_its_part_after_the_commit(
    logged_in_client, monkeypatch, tmp_path
):
    """The same deferral on the route that finalizes a single upload: the part goes only after its
    own commit. Nothing is left behind — the leak the deferral must not introduce."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        first = _an_unlinked_twin(client, TWIN)
        second_uid = _upload(client, "again.png", TWIN)
        done = client.post(f"/api/uploads/{second_uid}/finalize")
        assert done.status_code == 200, done.text
        assert done.json()["uid"] == first  # deduped onto the existing attachment
        assert not up_svc.part_path(second_uid).exists()
    finally:
        _cleanup()


async def _owner(adb, subject) -> User:
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def test_consume_records_instead_of_unlinking_when_the_caller_settles(
    adb, monkeypatch, tmp_path
):
    """`_consume` directly: with a list to record into it touches the filesystem not at all, and
    without one it keeps the old immediate unlink for a caller that has nothing to roll back to."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    part = up_svc.part_path("deadbeefdeadbeef")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(TWIN)

    recorded: list[str] = []
    await up_svc._consume(part, recorded)
    assert recorded == [str(part)]
    assert part.read_bytes() == TWIN  # still promised by a row this transaction may restore

    await up_svc._consume(part, None)
    assert not part.exists()


async def test_finalize_hands_a_dedup_hit_to_the_caller_to_settle(adb, monkeypatch, tmp_path):
    """The service-level pin: a dedup hit returns the existing attachment, deletes the upload row
    (transactionally, so a rollback brings it back) and leaves the bytes for the caller."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    owner = await _owner(adb, "dedup_defer")
    original = await up_svc.create_upload(
        adb, owner_id=owner.id, filename="a.png", mime="image/png", size=len(TWIN)
    )
    await up_svc.append_chunk(adb, original, 0, TWIN)
    first = await up_svc.finalize(adb, original, owner.id)
    assert first.sha256 == hashlib.sha256(TWIN).hexdigest()

    twin = await up_svc.create_upload(
        adb, owner_id=owner.id, filename="b.png", mime="image/png", size=len(TWIN)
    )
    await up_svc.append_chunk(adb, twin, 0, TWIN)
    consumed: list[str] = []
    again = await up_svc.finalize(adb, twin, owner.id, consumed_out=consumed)

    assert again.id == first.id  # the dedup hit
    assert consumed == [str(up_svc.part_path(twin.upload_uid))]
    assert up_svc.part_path(twin.upload_uid).read_bytes() == TWIN
    from app.models import Upload

    rows = (await adb.execute(select(Upload).where(Upload.upload_uid == twin.upload_uid))).all()
    assert rows == []  # the row is gone with THIS transaction, and comes back with its rollback
