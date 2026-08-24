"""MEMNG-124: an edit key makes a retried memo EDIT safe.

The create side got this in MEMNG-110. The edit side had nothing, and since MEMNG-113 that gap
bites hard: `PATCH /api/memos/{uid}` finalizes the upload sessions it names, so a client whose 2xx
never arrived cannot repeat the request — the replay's `upload_uids` are consumed, and the strict
check answers 409 `uploads_unlinkable` for an edit that IS stored, with the media attached. That is
precisely the retry an idempotency handle exists to protect, which is why the iOS outbox keeps
media edits foreground-only.

`edit_key` closes it. Its semantics deliberately differ from create's in one way worth stating out
loud, because a client depends on it:

    A replay returns the memo's CURRENT state, not a snapshot of what the original edit produced.

The original edit already landed; the key records *that it landed*, not what the row looked like
afterwards. So a replay arriving after some later write is answered with the row as it stands now.
That is the only answer that can be given without storing response bodies, and it is the one a
retrying client wants: "your edit is applied; here is the memo".
"""

import asyncio
import uuid

import pytest
from app.config import settings
from app.services import memos as memo_svc
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_uploads import _cleanup, _drop_user, _login_as, _reap_expired_committed, _start


def _key() -> str:
    """A canonical UUID — the shape a client actually mints, and unique per test so tests sharing
    one database cannot read each other's rows."""
    return str(uuid.uuid4())


def _edit_keys(key: str) -> list[tuple[str, str]]:
    """(memo uid, owner subject) for every edit recorded under `key`, read straight from the table.

    The authoritative answer to "how many times was this applied" — the API response cannot tell a
    fresh application apart from a replay, which is the whole point of the feature."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT m.uid, u.subject FROM memo_edit_keys k"
                " JOIN memos m ON m.id = k.memo_id"
                " JOIN users u ON u.id = k.owner_id"
                " WHERE k.edit_key = :k ORDER BY k.id"
            ),
            {"k": key},
        ).all()
    eng.dispose()
    return [(memo_uid, subject) for memo_uid, subject in rows]


def _edit_keys_of(memo_uid: str) -> list[str]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = (
            c.execute(
                text(
                    "SELECT k.edit_key FROM memo_edit_keys k JOIN memos m ON m.id = k.memo_id"
                    " WHERE m.uid = :u ORDER BY k.id"
                ),
                {"u": memo_uid},
            )
            .scalars()
            .all()
        )
    eng.dispose()
    return list(rows)


def _delete_memo(memo_uid: str) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM memos WHERE uid = :u"), {"u": memo_uid})
    eng.dispose()


def _memo(client, content="before", visibility="PRIVATE") -> str:
    posted = client.post("/api/memos", json={"content": content, "visibility": visibility})
    assert posted.status_code == 201, posted.text
    return posted.json()["uid"]


def _upload(client, name="edit.png", body=b"\x89PNG\r\n\x1a\n" + b"E" * 4000) -> str:
    uid = _start(client, name, "image/png", len(body))
    client.patch(f"/api/uploads/{uid}", content=body, headers={"Upload-Offset": "0"})
    return uid


# --- the replay contract -------------------------------------------------------------------------


def test_a_replayed_edit_is_not_applied_twice_and_answers_with_the_current_memo(logged_in_client):
    """The documented semantics, pinned: a replay reports the CURRENT row, it does not re-run.

    A later write is interposed between the edit and its replay, which is what separates the two
    possible readings apart. Re-applying would resurrect "v2" and lose "v3"; answering from a stored
    snapshot would report "v2" while the stored memo says "v3". Only "return the row as it stands"
    passes.
    """
    client, _ = logged_in_client
    key = _key()
    uid = _memo(client, "v1")
    try:
        edit = client.patch(f"/api/memos/{uid}", json={"content": "v2", "edit_key": key})
        assert edit.status_code == 200, edit.text
        assert edit.json()["content"] == "v2"

        later = client.patch(f"/api/memos/{uid}", json={"content": "v3"})  # someone edits again
        assert later.status_code == 200 and later.json()["content"] == "v3"

        replay = client.patch(f"/api/memos/{uid}", json={"content": "v2", "edit_key": key})
        assert replay.status_code == 200, replay.text
        assert replay.json()["content"] == "v3"  # current state, NOT a re-application of "v2"
        assert replay.json()["updated_at"] == later.json()["updated_at"]  # the row was not touched
        assert client.get(f"/api/memos/{uid}").json()["content"] == "v3"
        assert _edit_keys(key) == [(uid, "test_session_user")]  # applied exactly once
    finally:
        _delete_memo(uid)


def test_a_replay_after_the_uploads_were_consumed_returns_the_edit_instead_of_the_strict_409(
    logged_in_client, monkeypatch, tmp_path
):
    """The ticket, end to end. The first call finalized the upload sessions; by the time the retry
    arrives they are gone, so MEMNG-113's strict check would refuse it — for an edit that IS stored,
    with the photo attached. The key has to be answered BEFORE the uploads are looked at, and the
    replay must not link a second copy of the media."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    key = _key()
    uid = _memo(client, "text only")
    try:
        up = _upload(client)
        body = {"content": "text plus a photo", "upload_uids": [up], "edit_key": key}
        first = client.patch(f"/api/memos/{uid}", json=body)
        assert first.status_code == 200, first.text
        assert len(first.json()["attachments"]) == 1

        _reap_expired_committed()  # whatever the first call left is now definitively gone
        assert client.head(f"/api/uploads/{up}").status_code == 404  # premise for the retry

        replay = client.patch(f"/api/memos/{uid}", json=body)
        assert replay.status_code == 200, replay.text  # NOT the 409 the bare uploads would earn
        assert [a["uid"] for a in replay.json()["attachments"]] == [
            a["uid"] for a in first.json()["attachments"]
        ]  # the same one photo — not a second copy, not none
        assert _edit_keys(key) == [(uid, "test_session_user")]
    finally:
        _delete_memo(uid)
        _cleanup()


def test_without_a_key_a_repeated_media_edit_still_gets_the_strict_409(
    logged_in_client, monkeypatch, tmp_path
):
    """The unchanged path. A keyless PATCH is exactly what it was before this feature: the second
    call names an upload the first one consumed, and MEMNG-113 refuses it. Anything else would mean
    the new code changed behaviour for the SPA, which sends no key."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    uid = _memo(client, "text only")
    try:
        up = _upload(client)
        body = {"content": "with a photo", "upload_uids": [up]}
        first = client.patch(f"/api/memos/{uid}", json=body)
        assert first.status_code == 200 and len(first.json()["attachments"]) == 1

        second = client.patch(f"/api/memos/{uid}", json=body)
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "uploads_unlinkable"
        assert second.json()["detail"]["missing_upload_uids"] == [up]
        assert _edit_keys_of(uid) == []  # a keyless edit records nothing at all
    finally:
        _delete_memo(uid)
        _cleanup()


def test_without_a_key_a_repeated_content_edit_still_applies_every_time(logged_in_client):
    # No key means no dedupe: two PATCHes are two edits, and the second one's content wins.
    client, _ = logged_in_client
    uid = _memo(client, "v1")
    try:
        assert client.patch(f"/api/memos/{uid}", json={"content": "v2"}).status_code == 200
        assert client.patch(f"/api/memos/{uid}", json={"content": "v3"}).status_code == 200
        assert client.get(f"/api/memos/{uid}").json()["content"] == "v3"
        assert _edit_keys_of(uid) == []
    finally:
        _delete_memo(uid)


def test_a_new_edit_naming_missing_uploads_still_gets_the_strict_409_and_records_nothing(
    logged_in_client,
):
    """The key must not weaken MEMNG-113 for a genuinely NEW edit. An unknown upload uid is still a
    409, and — because the whole request rolls back — the key is left unused, so the client's real
    retry (after re-uploading) is a fresh application and not a replay of nothing."""
    client, _ = logged_in_client
    key = _key()
    uid = _memo(client, "unchanged")
    try:
        refused = client.patch(
            f"/api/memos/{uid}",
            json={"content": "never applied", "upload_uids": ["no-such-upload"], "edit_key": key},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "uploads_unlinkable"
        assert refused.json()["detail"]["missing_upload_uids"] == ["no-such-upload"]
        assert _edit_keys(key) == []  # nothing recorded → the key is still free
        assert client.get(f"/api/memos/{uid}").json()["content"] == "unchanged"  # nothing applied

        # ...and the key still works afterwards, on the retry the client actually sends.
        retried = client.patch(
            f"/api/memos/{uid}", json={"content": "applied now", "edit_key": key}
        )
        assert retried.status_code == 200 and retried.json()["content"] == "applied now"
        assert _edit_keys(key) == [(uid, "test_session_user")]
    finally:
        _delete_memo(uid)


def test_two_owners_may_reuse_one_edit_key(logged_in_client, monkeypatch):
    # The key is the CLIENT's name for its own edit, so it is unique per owner, never globally.
    client, _ = logged_in_client
    key = _key()
    mine = _memo(client, "mine")
    theirs_uid = None
    try:
        theirs = _login_as(monkeypatch, "edit_other_owner")
        theirs_uid = _memo(theirs, "theirs")
        ours = client.patch(f"/api/memos/{mine}", json={"content": "mine edited", "edit_key": key})
        other = theirs.patch(
            f"/api/memos/{theirs_uid}", json={"content": "theirs edited", "edit_key": key}
        )
        assert ours.status_code == other.status_code == 200, (ours.text, other.text)
        assert ours.json()["content"] == "mine edited"
        assert other.json()["content"] == "theirs edited"  # not replayed off the other owner's row
        assert sorted(_edit_keys(key)) == sorted(
            [(mine, "test_session_user"), (theirs_uid, "edit_other_owner")]
        )
    finally:
        _delete_memo(mine)
        if theirs_uid:
            _delete_memo(theirs_uid)
        _drop_user("edit_other_owner")


def test_reusing_one_key_on_a_second_memo_is_refused_instead_of_replaying_the_first(
    logged_in_client,
):
    """A key already bound to another of the owner's memos is a client bug, and the only two
    alternatives are both wrong: replaying would answer a PATCH of memo B with memo A's body (a
    different uid than the one in the path), and applying would leave the owner with one key naming
    two edits, so neither could ever be replayed. Refusing says so, in a code a client can switch
    on, and memo B is left untouched."""
    client, _ = logged_in_client
    key = _key()
    first, second = _memo(client, "first"), _memo(client, "second")
    try:
        bound = client.patch(f"/api/memos/{first}", json={"content": "a", "edit_key": key})
        assert bound.status_code == 200, bound.text
        clash = client.patch(f"/api/memos/{second}", json={"content": "b", "edit_key": key})
        assert clash.status_code == 409, clash.text
        assert clash.json()["detail"]["code"] == "edit_key_conflict"
        assert clash.json()["detail"]["memo_uid"] == first  # names the memo it is already bound to
        assert client.get(f"/api/memos/{second}").json()["content"] == "second"  # untouched
        assert _edit_keys(key) == [(first, "test_session_user")]
    finally:
        _delete_memo(first)
        _delete_memo(second)


def test_deleting_the_memo_takes_its_edit_keys_with_it(logged_in_client):
    """The recorded edits belong to the memo. Leaving them behind would keep rows pointing at a
    memo nobody has, and (worse) hold the key hostage against a later, legitimate edit."""
    client, _ = logged_in_client
    key = _key()
    uid = _memo(client, "temporary")
    client.patch(f"/api/memos/{uid}", json={"content": "edited", "edit_key": key})
    assert _edit_keys(key) == [(uid, "test_session_user")]
    assert client.delete(f"/api/memos/{uid}").status_code == 204
    assert _edit_keys(key) == []


@pytest.mark.parametrize("key", ["short", "has spaces here", "x" * 65, "no/slashes/allowed"])
def test_a_malformed_edit_key_is_a_plain_validation_error(logged_in_client, key):
    # Shape is bounded so the unique index stores something sane — and a malformed key stays an
    # ordinary 422, never the 409 that tells a client its uploads need re-sending.
    client, _ = logged_in_client
    uid = _memo(client, "shape check")
    try:
        refused = client.patch(f"/api/memos/{uid}", json={"content": "x", "edit_key": key})
        assert refused.status_code == 422
        assert client.get(f"/api/memos/{uid}").json()["content"] == "shape check"
    finally:
        _delete_memo(uid)


async def test_two_concurrent_edits_with_one_key_apply_exactly_one_of_them(pat_client, monkeypatch):
    """The race the unique index exists for: both requests read no recorded edit for the key, so
    both try to record one, and the index picks the winner. The loser must answer with the memo as
    the winner left it — never a second application, never a 500.

    The barrier makes that interleaving certain instead of likely: each request is held right after
    its replay lookup came back empty until the other has got that far too. The lookup itself is the
    real one; only its return is delayed. The loser's SECOND lookup (the read-back after it loses)
    must not wait — by then there is nobody left to meet.
    """
    bearer, subject, _ = pat_client
    created = await bearer.post("/api/memos", json={"content": "v1", "visibility": "PRIVATE"})
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]

    key = _key()
    lookups = 0
    both_past_the_lookup = asyncio.Barrier(2)
    real_lookup = memo_svc.get_memo_by_edit_key

    async def barriered(db, owner_id, edit_key):
        nonlocal lookups
        found = await real_lookup(db, owner_id, edit_key)
        lookups += 1
        if lookups <= 2:
            await both_past_the_lookup.wait()
        return found

    monkeypatch.setattr(memo_svc, "get_memo_by_edit_key", barriered)
    try:
        body = {"content": "double-tapped", "edit_key": key}
        first, second = await asyncio.wait_for(
            asyncio.gather(
                bearer.patch(f"/api/memos/{uid}", json=body),
                bearer.patch(f"/api/memos/{uid}", json=body),
            ),
            timeout=30,
        )
        # Both really did race — a run where one took the replay shortcut would leave this at 2
        # with no read-back, and would not have exercised the index at all.
        assert lookups == 3
        assert first.status_code == second.status_code == 200, (first.text, second.text)
        assert first.json()["content"] == second.json()["content"] == "double-tapped"
        # One row, one application: the loser reported the winner's row rather than writing again.
        assert _edit_keys(key) == [(uid, subject)]
        assert first.json()["updated_at"] == second.json()["updated_at"]
    finally:
        _delete_memo(uid)
