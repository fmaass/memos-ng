"""MEMNG-110: an idempotency key makes a retried memo create safe.

A client that dies between the server committing and the client recording the 201 has no way to
tell "stored" from "never arrived". Retrying without a key stores a second memo — every create
mints a fresh uid (services/memos.gen_uid). Since MEMNG-113 the duplicate is worse than a
double-post: the retry's upload_uids were consumed by the first attempt, so the second call is
refused outright. The key is what turns that retry back into the answer the client was missing.
"""

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from app.config import settings
from app.models import CREATE_KEY_CONSTRAINT, Memo, User, Visibility
from app.routers.memos import _create_or_replay
from app.services import memos as memo_svc
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import SYNC_URL
from tests.test_memo_edit_idempotency import _FakeAsyncpgError, _wrapped
from tests.test_uploads import _cleanup, _drop_user, _login_as, _reap_expired_committed, _start

KEY = "b3f1c2d4-5e6a-47b8-9c0d-1e2f3a4b5c6d"  # a canonical UUID, the shape a client will send


def _memo_uids_with_key(key: str) -> list[str]:
    """Every memo stored under `key`, read straight from the table. The authoritative answer to
    "did a second memo get created" — the feed would also have to be paged to prove absence."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(
            text("SELECT uid FROM memos WHERE idempotency_key = :k ORDER BY id"), {"k": key}
        ).scalars().all()
    eng.dispose()
    return list(rows)


def _delete_memos_with_key(key: str) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM memos WHERE idempotency_key = :k"), {"k": key})
    eng.dispose()


def test_the_same_key_twice_stores_one_memo_and_answers_with_it_both_times(logged_in_client):
    client, _ = logged_in_client
    try:
        body = {"content": "posted once", "visibility": "PUBLIC", "idempotency_key": KEY}
        first, second = client.post("/api/memos", json=body), client.post("/api/memos", json=body)
        assert first.status_code == second.status_code == 201  # the replay keeps the 201
        assert first.json()["uid"] == second.json()["uid"]
        assert first.json() == second.json()  # the replay is the stored memo, not a fresh shell
        assert _memo_uids_with_key(KEY) == [first.json()["uid"]]
    finally:
        _delete_memos_with_key(KEY)


def test_a_create_without_a_key_still_stores_one_memo_per_call(logged_in_client):
    # The unchanged path: no key means no dedupe, and two identical posts are two memos, exactly as
    # before. Anything else would silently swallow a user's deliberate double-post.
    client, _ = logged_in_client
    body = {"content": "twice on purpose", "visibility": "PUBLIC"}
    first, second = client.post("/api/memos", json=body), client.post("/api/memos", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["uid"] != second.json()["uid"]
    assert _memo_uids_with_key(KEY) == []  # and nothing was filed under a key at all


def test_two_owners_may_reuse_one_key(logged_in_client, monkeypatch):
    # The key is the CLIENT's name for its own create, so it is unique per owner, never globally.
    # Two phones that both mint the same token must not collide into one memo.
    mine, _ = logged_in_client
    try:
        theirs = _login_as(monkeypatch, "idem_other_owner")
        body = {"content": "same token, different people", "idempotency_key": KEY}
        ours, others = mine.post("/api/memos", json=body), theirs.post("/api/memos", json=body)
        assert ours.status_code == others.status_code == 201
        assert ours.json()["uid"] != others.json()["uid"]
        assert sorted(_memo_uids_with_key(KEY)) == sorted(
            [ours.json()["uid"], others.json()["uid"]]
        )
    finally:
        _delete_memos_with_key(KEY)
        _drop_user("idem_other_owner")


def test_replaying_after_the_uploads_were_reaped_returns_the_original_and_its_attachments(
    logged_in_client, monkeypatch, tmp_path
):
    # The two tickets meeting. The first call consumed the upload sessions; by the time the retry
    # arrives they are gone, so MEMNG-113 would refuse it with a 409 — for a memo that IS stored,
    # with the media attached. The key has to be answered BEFORE the uploads are looked at.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"R" * 4000
        uid = _start(client, "kept.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
        body = {
            "content": "with my only copy",
            "visibility": "PUBLIC",
            "upload_uids": [uid],
            "idempotency_key": KEY,
        }
        first = client.post("/api/memos", json=body)
        assert first.status_code == 201 and len(first.json()["attachments"]) == 1

        _reap_expired_committed()  # anything the first call left is now definitively gone
        assert client.head(f"/api/uploads/{uid}").status_code == 404  # premise for the retry

        replay = client.post("/api/memos", json=body)
        assert replay.status_code == 201, replay.text  # NOT the 409 the bare uploads would earn
        assert replay.json()["uid"] == first.json()["uid"]
        assert [a["uid"] for a in replay.json()["attachments"]] == [
            a["uid"] for a in first.json()["attachments"]
        ]
        assert _memo_uids_with_key(KEY) == [first.json()["uid"]]
    finally:
        _delete_memos_with_key(KEY)
        _cleanup()


@pytest.mark.parametrize(
    "key",
    ["short", "has spaces here", "x" * 65, "no/slashes/allowed"],
)
def test_a_malformed_key_is_a_plain_validation_error(logged_in_client, key):
    # Shape is bounded so the unique index stores something sane — and a malformed key stays an
    # ordinary 422, never the 409 that tells a client its uploads need re-sending.
    client, _ = logged_in_client
    posted = client.post("/api/memos", json={"content": "x", "idempotency_key": key})
    assert posted.status_code == 422


async def test_two_concurrent_creates_with_one_key_store_exactly_one_memo(pat_client, monkeypatch):
    # The race the partial unique index exists for: both requests read no memo for the key, so both
    # try to insert, and the index picks the winner. The loser must answer with the winner's memo.
    #
    # The barrier makes that interleaving certain instead of likely: it holds each request inside
    # create_memo — after its replay lookup came back empty, before its INSERT — until the other
    # has got that far too. The real tag lookup still runs; only its start is delayed.
    bearer, _, _ = pat_client
    entered = 0
    both_past_the_lookup = asyncio.Barrier(2)
    real_get_or_create_tags = memo_svc._get_or_create_tags

    async def barriered(db, names):
        nonlocal entered
        entered += 1
        await both_past_the_lookup.wait()
        return await real_get_or_create_tags(db, names)

    monkeypatch.setattr(memo_svc, "_get_or_create_tags", barriered)
    try:
        body = {"content": "double-tapped", "visibility": "PUBLIC", "idempotency_key": KEY}
        first, second = await asyncio.wait_for(
            asyncio.gather(
                bearer.post("/api/memos", json=body), bearer.post("/api/memos", json=body)
            ),
            timeout=30,
        )
        # Both really did race — a run where one took the replay shortcut would leave this at 1 and
        # would not have exercised the index at all.
        assert entered == 2
        assert first.status_code == second.status_code == 201, (first.text, second.text)
        assert first.json()["uid"] == second.json()["uid"]
        assert _memo_uids_with_key(KEY) == [first.json()["uid"]]
    finally:
        _delete_memos_with_key(KEY)


async def test_a_foreign_conflict_that_self_heals_still_stores_both_memos(pat_client, monkeypatch):
    """Two creates with DISTINCT keys, colliding on the tags index — not on the key at all.

    Both posts invent the same brand-new hashtag, so `_get_or_create_tags` has them both select
    nothing and both insert; one loses on `ix_tags_name`. That is the loser's OWN write
    conflicting, and it is TRANSIENT: by the time the loser tries again the winner's tag is
    committed, so the retry finds it and the memo lands. Both memos are stored.

    This is the regression guard for MEMNG-131: narrowing `replayable` to the key constraint —
    the obvious way to stop foreign conflicts being called a raced create — would delete that
    self-heal and answer the loser with a bare IntegrityError, losing a memo a user did post.
    The fix therefore leaves the retry alone and discriminates only where the retries run out.

    The barrier holds each request between its tag SELECT and its tag INSERT until the other has
    got there too, which is what makes both of them try the insert instead of one reusing the
    other's committed tag.
    """
    bearer, _, _ = pat_client
    tag = f"race{uuid.uuid4().hex[:12]}"
    keys = (str(uuid.uuid4()), str(uuid.uuid4()))  # DISTINCT: nothing here touches the key index
    entered = 0
    both_past_the_select = asyncio.Barrier(2)
    real_get_or_create_tags = memo_svc._get_or_create_tags

    async def barriered(db, names):
        nonlocal entered
        entered += 1
        if entered <= 2:  # only the first pair is held; the loser's retry must not wait for a peer
            await both_past_the_select.wait()
        return await real_get_or_create_tags(db, names)

    monkeypatch.setattr(memo_svc, "_get_or_create_tags", barriered)
    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                bearer.post(
                    "/api/memos", json={"content": f"#{tag} a", "idempotency_key": keys[0]}
                ),
                bearer.post(
                    "/api/memos", json={"content": f"#{tag} b", "idempotency_key": keys[1]}
                ),
                return_exceptions=True,
            ),
            timeout=30,
        )
        # entered == 3: both reached the insert, and the loser really did take a second run at it.
        assert entered == 3, entered
        raised = [r for r in (first, second) if isinstance(r, BaseException)]
        assert raised == [], raised  # neither create may die on someone else's constraint
        codes = sorted(r.status_code for r in (first, second))
        assert codes == [201, 201], (codes, first.text, second.text)
        assert _memo_uids_with_key(keys[0]) and _memo_uids_with_key(keys[1])  # both really stored
    finally:
        for k in keys:
            _delete_memos_with_key(k)
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM tags WHERE name = :n"), {"n": tag})
        eng.dispose()


# --- the lost-race resolution, as control flow ---------------------------------------------------
# Driving these through the DB would need a concurrent create AND a concurrent delete of that
# create to land inside one microsecond-wide window, twice over. The loop's three dependencies are
# injected instead — the attempt, the read-back and the rollback are what the loop CALLS, not what
# it is — so every branch, including the exhausted one, is reachable and its call order is visible.
# The real race stays covered end-to-end by the concurrent-create test above.


def _collision() -> IntegrityError:
    """A collision on the KEY — the one conflict a same-key retry can actually resolve.

    Shaped the way the driver delivers it (the constraint name on the asyncpg error under
    `__cause__`), because the loop reads that name when its retries run out to decide whether
    "retry with the same idempotency_key" is honest advice."""
    return _wrapped(Exception("wrapper"), _FakeAsyncpgError(CREATE_KEY_CONSTRAINT))


def _foreign_collision() -> IntegrityError:
    """Someone else's constraint — the tags index is the one this really happens on."""
    return _wrapped(Exception("wrapper"), _FakeAsyncpgError("ix_tags_name"))


class _Loop:
    """Scripted stand-ins for attempt/lookup/rollback that record the order they were called in."""

    def __init__(self, attempts: list, lookups: list | None = None) -> None:
        self._attempts = list(attempts)
        self._lookups = list(lookups or [])
        self.calls: list[str] = []

    async def attempt(self) -> Memo:
        self.calls.append("attempt")
        outcome = self._attempts.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def lookup(self) -> Memo | None:
        self.calls.append("lookup")
        return self._lookups.pop(0)

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def run(self) -> Memo:
        return await _create_or_replay(
            attempt=self.attempt, lookup=self.lookup, rollback=self.rollback
        )


async def test_a_create_that_lands_is_returned_untouched():
    stored = Memo(uid="first-try")
    loop = _Loop([stored])
    assert await loop.run() is stored
    assert loop.calls == ["attempt"]  # no rollback, no read-back on the happy path


async def test_losing_the_race_returns_the_winner_without_inserting_again():
    winner = Memo(uid="the-winner")
    loop = _Loop([_collision()], [winner])
    assert await loop.run() is winner
    # the insert is NOT retried while a winner exists — that would be the duplicate memo the key
    # is there to prevent
    assert loop.calls == ["attempt", "rollback", "lookup"]


async def test_a_winner_deleted_before_the_read_back_is_retried_rather_than_500ing():
    # The blocking case: we collided, so a row existed; by the time we read it back it was gone.
    # The key is free again, so a fresh insert is legitimate and must be taken.
    second = Memo(uid="second-try")
    loop = _Loop([_collision(), second], [None])
    assert await loop.run() is second
    assert loop.calls == ["attempt", "rollback", "lookup", "attempt"]


async def test_a_winner_that_appears_on_the_second_round_is_returned():
    winner = Memo(uid="late-winner")
    loop = _Loop([_collision(), _collision()], [None, winner])
    assert await loop.run() is winner
    assert loop.calls == ["attempt", "rollback", "lookup", "attempt", "rollback", "lookup"]


async def test_exhausting_the_retries_is_a_503_the_client_may_repeat():
    # The positive control for the two tests below: when the KEY is what kept colliding, the 503
    # and its "retry with the same idempotency_key" are honest, and must survive the narrowing.
    loop = _Loop([_collision(), _collision()], [None, None])
    with pytest.raises(HTTPException) as raised:
        await loop.run()
    assert raised.value.status_code == 503  # never a bare IntegrityError surfacing as a 500
    assert raised.value.detail["code"] == "create_raced"
    assert "idempotency_key" in raised.value.detail["message"]
    assert loop.calls.count("attempt") == 2  # bounded: no hot loop on a pathological race


async def test_a_foreign_conflict_that_never_heals_surfaces_instead_of_same_key_retry_advice():
    """The mislabel MEMNG-131 removes: retries spent on someone ELSE's constraint.

    A foreign conflict is still worth retrying — usually it self-heals, which the tags race above
    proves end to end. What it must never do is run out of retries and be reported as a raced
    create: "retry with the same idempotency_key" names a remedy that cannot work, and buries a
    real fault that surfaced plainly before the key existed. When the retries are gone, the error
    that actually kept happening is the answer."""
    boom = _foreign_collision()
    loop = _Loop([_foreign_collision(), boom], [None, None])
    with pytest.raises(IntegrityError) as raised:
        await loop.run()
    assert raised.value is boom  # the real conflict, not a 503 telling the client to repeat itself
    assert loop.calls.count("attempt") == 2  # the retry still happened — the self-heal is intact


async def test_an_unnamed_integrity_error_at_exhaustion_surfaces_too():
    # Fail closed: a driver that will not say what was violated cannot be claimed as the key, so
    # the error propagates rather than becoming retry advice that may be nonsense.
    boom = IntegrityError("INSERT INTO memos ...", {}, Exception("duplicate key value"))
    loop = _Loop([boom, boom], [None, None])
    with pytest.raises(IntegrityError) as raised:
        await loop.run()
    assert raised.value is boom


async def test_any_other_failure_propagates_with_no_recovery_attempted():
    # Only a unique-index collision means "someone else got there first". Anything else is a real
    # fault and must not be swallowed into a retry.
    boom = RuntimeError("disk fell over")
    loop = _Loop([boom])
    with pytest.raises(RuntimeError):
        await loop.run()
    assert loop.calls == ["attempt"]


async def test_the_replay_lookup_holds_a_concurrent_delete_off_until_it_has_read_the_memo(
    db_engine,
):
    """A replay answers 201 with the WHOLE memo, so the read cannot be torn in half.

    The relationships load as separate statements, and under READ COMMITTED each one sees a fresh
    snapshot — a delete committing between them would produce a 201 carrying a memo with some or
    none of its attachments. The share lock is what makes that impossible, and the only way to
    prove it is to hold the lock and watch a real DELETE queue behind it.
    """
    from app.database import async_session

    subject = "idem_lock_owner"
    async with async_session() as setup:
        owner = User(subject=subject, role="user")
        setup.add(owner)
        await setup.flush()
        memo = Memo(
            uid="lock-probe-memo",
            owner_id=owner.id,
            content="read me whole",
            visibility=Visibility.PRIVATE,
            idempotency_key=KEY,
        )
        setup.add(memo)
        await setup.commit()
        owner_id, memo_id = owner.id, memo.id
    try:
        async with async_session() as reader:
            found = await memo_svc.get_by_idempotency_key(reader, owner_id, KEY)
            assert found is not None and found.id == memo_id

            async with async_session() as deleter:
                deleting = asyncio.create_task(
                    deleter.execute(delete(Memo).where(Memo.id == memo_id))
                )
                # Still blocked a second later: the delete has to take a conflicting lock on this
                # same row before its cascade can touch a single child.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(deleting), timeout=1.0)

                await reader.rollback()  # the replay is done reading — release the share lock
                await asyncio.wait_for(deleting, timeout=10)  # ...and the delete goes through
                await deleter.commit()
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM memos WHERE uid = 'lock-probe-memo'"))
            c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        eng.dispose()


def _insert_racing_winner(owner_id: int, key: str) -> None:
    """A concurrent create landing under the same key, committed from another connection."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned, idempotency_key)"
                " VALUES ('racing-winner-memo', :o, 'raced in', 'PRIVATE', false, :k)"
            ),
            {"o": owner_id, "k": key},
        )
    eng.dispose()


def _attachment_bytes(att_uid: str) -> bytes:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        path = c.execute(
            text("SELECT storage_path FROM attachments WHERE uid = :u"), {"u": att_uid}
        ).scalar_one()
    eng.dispose()
    return Path(path).read_bytes()


def test_a_vanished_winner_is_retried_with_its_media_intact(
    logged_in_client, monkeypatch, tmp_path
):
    """The retry path carrying uploads — where deleting the moved originals would be unrecoverable.

    Attempt 1 finalizes the upload (moving its part file), then collides with a memo a concurrent
    create landed under the same key. That attempt rolls back, which returns the upload row to
    pending — so the bytes have to be back at the part file too, or the retry re-finalizes an
    upload whose file is gone and the photo is lost for good. The winner is then deleted before it
    can be read back, which frees the key, and the retry must produce a COMPLETE memo.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    real_lookup = memo_svc.get_by_idempotency_key
    lookups = 0

    async def racing_lookup(db, owner_id, key):
        # Orchestrates WHEN the two concurrent writes land; the lookup itself is the real one.
        nonlocal lookups
        lookups += 1
        if lookups == 2:  # the winner is deleted before this read-back can see it
            _delete_memos_with_key(key)
        found = await real_lookup(db, owner_id, key)
        if lookups == 1:  # ...and a rival create lands just after our pre-check came back empty
            _insert_racing_winner(owner_id, key)
        return found

    try:
        data = b"\x89PNG\r\n\x1a\n" + b"V" * 4000
        uid = _start(client, "precious.png", "image/png", len(data))
        client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})

        monkeypatch.setattr(memo_svc, "get_by_idempotency_key", racing_lookup)
        posted = client.post(
            "/api/memos",
            # visibility deliberately OMITTED: the effective one then comes off the ORM user, the
            # exact read that must have been hoisted out of the retried closure.
            json={"content": "my only copy", "upload_uids": [uid], "idempotency_key": KEY},
        )
        assert posted.status_code == 201, posted.text
        assert lookups == 2  # the collision and the vanished read-back both really happened
        attachments = posted.json()["attachments"]
        assert len(attachments) == 1  # the retry kept the media, it did not save the text alone
        assert (
            hashlib.sha256(_attachment_bytes(attachments[0]["uid"])).hexdigest()
            == hashlib.sha256(data).hexdigest()
        )  # ...and the stored bytes are the uploaded bytes, not a truncated or empty file
        assert _memo_uids_with_key(KEY) == [posted.json()["uid"]]
    finally:
        _delete_memos_with_key(KEY)
        _cleanup()
