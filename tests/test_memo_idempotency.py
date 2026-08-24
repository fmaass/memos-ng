"""MEMNG-110: an idempotency key makes a retried memo create safe.

A client that dies between the server committing and the client recording the 201 has no way to
tell "stored" from "never arrived". Retrying without a key stores a second memo — every create
mints a fresh uid (services/memos.gen_uid). Since MEMNG-113 the duplicate is worse than a
double-post: the retry's upload_uids were consumed by the first attempt, so the second call is
refused outright. The key is what turns that retry back into the answer the client was missing.
"""

import asyncio

import pytest
from app.config import settings
from app.services import memos as memo_svc
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
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
