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
from app.models import EDIT_KEY_CONSTRAINT, Memo
from app.routers.memos import _apply_or_replay, _is_edit_key_collision, violated_constraint
from app.services import memos as memo_svc
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

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


def _edit_key_row_count(key: str) -> int:
    """Rows in memo_edit_keys under `key`, read DIRECTLY — deliberately no join through memos.

    `_edit_keys` above joins, which is right when the claim is "bound to that memo, owned by that
    user". It is exactly wrong for the claim "the row is gone": against a schema that lost its
    ON DELETE CASCADE the orphans would still be there, pointing at a memo that no longer exists,
    and an INNER join through memos drops precisely those rows — reporting empty while the leak it
    exists to catch accumulates."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        found = c.execute(
            text("SELECT count(*) FROM memo_edit_keys WHERE edit_key = :k"), {"k": key}
        ).scalar_one()
    eng.dispose()
    return int(found)


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
        assert _edit_key_row_count(key) == 0  # nothing recorded at all → the key is still free
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
    # Counted straight off memo_edit_keys: an orphan left behind by a missing cascade is invisible
    # to any query that reaches the row THROUGH the memo it no longer has.
    assert _edit_key_row_count(key) == 0


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


# --- the replay path answers ONE conflict, and only that one --------------------------------------


async def test_a_race_on_someone_elses_constraint_is_not_dressed_up_as_a_raced_edit(
    pat_client, monkeypatch
):
    """Two edits with DISTINCT keys, colliding on the tags index — not on the edit key at all.

    Both patches introduce the same brand-new hashtag, so `_get_or_create_tags` has them both
    select nothing and both insert; one loses on `ix_tags_name`. That is the loser's OWN write
    conflicting, not a lost race for an edit key: absorbing it would answer "retry with the same
    edit_key" for a conflict retrying cannot fix, and would bury a fault that surfaced plainly
    before this feature existed. It must propagate exactly as it always did.

    The barrier holds each request between its tag SELECT and its tag INSERT until the other has
    got there too, which is what makes both of them try the insert instead of one reusing the
    other's committed tag.
    """
    bearer, _, _ = pat_client
    first_uid = (await bearer.post("/api/memos", json={"content": "one"})).json()["uid"]
    second_uid = (await bearer.post("/api/memos", json={"content": "two"})).json()["uid"]
    tag = f"race{uuid.uuid4().hex[:12]}"
    keys = (_key(), _key())  # DISTINCT: nothing here should ever touch the edit-key constraint
    entered = 0
    both_past_the_select = asyncio.Barrier(2)
    real_get_or_create_tags = memo_svc._get_or_create_tags

    async def barriered(db, names):
        nonlocal entered
        entered += 1
        if entered <= 2:
            await both_past_the_select.wait()
        return await real_get_or_create_tags(db, names)

    monkeypatch.setattr(memo_svc, "_get_or_create_tags", barriered)
    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                bearer.patch(
                    f"/api/memos/{first_uid}", json={"content": f"#{tag} a", "edit_key": keys[0]}
                ),
                bearer.patch(
                    f"/api/memos/{second_uid}", json={"content": f"#{tag} b", "edit_key": keys[1]}
                ),
                return_exceptions=True,
            ),
            timeout=30,
        )
        assert entered == 2  # both really did reach the tag insert; nothing took a shortcut
        codes = sorted(
            r.status_code for r in (first, second) if not isinstance(r, BaseException)
        )
        # One edit lands. The loser's tag collision is NOT laundered into 503 edit_raced — the
        # single answer this test exists to forbid.
        outcomes = (codes, getattr(first, "text", first), getattr(second, "text", second))
        assert 503 not in codes, outcomes
        assert 200 in codes, codes
    finally:
        _delete_memo(first_uid)
        _delete_memo(second_uid)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM tags WHERE name = :n"), {"n": tag})
        eng.dispose()


class _FakeAsyncpgError(Exception):
    """Stands in for asyncpg's UniqueViolationError: the driver dependency, not the unit."""

    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("duplicate key value")
        self.constraint_name = constraint_name


class _FakeDiag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _FakePsycopgError(Exception):
    """Stands in for psycopg2's error, which carries the name on `diag` instead."""

    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("duplicate key value")
        self.diag = _FakeDiag(constraint_name)


def _wrapped(orig: Exception, cause: Exception | None = None) -> IntegrityError:
    """An IntegrityError shaped like the driver produces: SQLAlchemy's shim on top, the driver's own
    exception on `__cause__` (verified against the live asyncpg stack, which puts the constraint
    name there and NOT on `orig`)."""
    if cause is not None:
        orig.__cause__ = cause
    return IntegrityError("INSERT INTO ...", {}, orig)


def test_the_constraint_name_is_read_from_where_asyncpg_actually_puts_it():
    exc = _wrapped(Exception("wrapper"), _FakeAsyncpgError(EDIT_KEY_CONSTRAINT))
    assert violated_constraint(exc) == EDIT_KEY_CONSTRAINT
    assert _is_edit_key_collision(exc)


def test_the_psycopg_diag_shape_is_read_too():
    exc = _wrapped(_FakePsycopgError(EDIT_KEY_CONSTRAINT))
    assert violated_constraint(exc) == EDIT_KEY_CONSTRAINT
    assert _is_edit_key_collision(exc)


def test_a_foreign_constraint_is_not_mistaken_for_an_edit_key_collision():
    # The real one this guards: `_get_or_create_tags` racing on the tags unique index.
    exc = _wrapped(Exception("wrapper"), _FakeAsyncpgError("ix_tags_name"))
    assert violated_constraint(exc) == "ix_tags_name"
    assert not _is_edit_key_collision(exc)


def test_an_error_that_names_no_constraint_is_not_ours():
    # Fail closed: an unrecognised integrity error propagates rather than being absorbed into a
    # retry path that cannot resolve it.
    exc = _wrapped(Exception("duplicate key value"))
    assert violated_constraint(exc) is None
    assert not _is_edit_key_collision(exc)


async def test_the_loop_reraises_what_the_discriminator_rejects():
    """The wiring, at the level the router uses it: a rejected error never reaches rollback/lookup.

    Driving this through the DB would need the tag race above; here the three injected dependencies
    make the control flow itself visible — that the raise happens BEFORE any recovery is attempted
    is the whole point of the narrowing.
    """
    calls: list[str] = []
    boom = _wrapped(Exception("wrapper"), _FakeAsyncpgError("ix_tags_name"))

    async def attempt() -> Memo:
        calls.append("attempt")
        raise boom

    async def lookup() -> Memo | None:
        calls.append("lookup")
        return Memo(uid="never-read")

    async def rollback() -> None:
        calls.append("rollback")

    with pytest.raises(IntegrityError) as raised:
        await _apply_or_replay(
            attempt=attempt,
            lookup=lookup,
            rollback=rollback,
            tries=2,
            replayable=_is_edit_key_collision,
            exhausted=HTTPException(status_code=503, detail="unreachable"),
        )
    assert raised.value is boom  # the original error, not a 500-shaped substitute
    assert calls == ["attempt"]  # no rollback, no read-back, no second try


async def test_the_loop_still_recovers_from_its_own_constraint():
    """The positive control for the test above: with the SAME loop and the same shapes, an edit-key
    collision does take the replay path. Without this, a `replayable` that returned False for
    everything would pass every assertion in the negative test."""
    calls: list[str] = []
    winner = Memo(uid="the-winner")

    async def attempt() -> Memo:
        calls.append("attempt")
        raise _wrapped(Exception("wrapper"), _FakeAsyncpgError(EDIT_KEY_CONSTRAINT))

    async def lookup() -> Memo | None:
        calls.append("lookup")
        return winner

    async def rollback() -> None:
        calls.append("rollback")

    settled = await _apply_or_replay(
        attempt=attempt,
        lookup=lookup,
        rollback=rollback,
        tries=1,
        replayable=_is_edit_key_collision,
        exhausted=HTTPException(status_code=503, detail="unreachable"),
    )
    assert settled is winner
    assert calls == ["attempt", "rollback", "lookup"]
