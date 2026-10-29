"""MEMNG-1036: a memo carries the key it was created under — to its owner, and to nobody else.

An outbox whose create failed with no answer cannot tell "stored" from "never arrived" by looking
at the timeline unless the timeline names the create. The key is that name. It is the owner's
private retry token, so every other reader sees null: a reader who learned it could not replay the
create (the replay lookup is scoped to the owner), but nothing they hold needs it either.
"""

from tests.test_memo_idempotency import _delete_memos_with_key
from tests.test_uploads import _drop_user, _login_as

KEY = "9d0c1b2a-3f4e-45d6-87a8-b9c0d1e2f3a4"
OTHER = "idem_key_reader"


def _in_list(client, uid: str) -> dict:
    page = client.get("/api/memos")
    assert page.status_code == 200
    found = [m for m in page.json()["items"] if m["uid"] == uid]
    assert len(found) == 1, f"{uid} not on the first page of the feed"
    return found[0]


def test_the_owner_reads_back_the_key_the_memo_was_created_under(logged_in_client):
    client, _ = logged_in_client
    try:
        created = client.post(
            "/api/memos", json={"content": "keyed", "visibility": "PRIVATE", "idempotency_key": KEY}
        )
        assert created.status_code == 201
        uid = created.json()["uid"]
        assert created.json()["idempotency_key"] == KEY

        one = client.get(f"/api/memos/{uid}")
        assert one.status_code == 200
        assert one.json()["idempotency_key"] == KEY
        assert _in_list(client, uid)["idempotency_key"] == KEY
    finally:
        _delete_memos_with_key(KEY)


def test_another_member_reading_the_same_public_memo_sees_null(logged_in_client, monkeypatch):
    owner, _ = logged_in_client
    try:
        created = owner.post(
            "/api/memos", json={"content": "shared", "visibility": "PUBLIC", "idempotency_key": KEY}
        )
        assert created.status_code == 201
        uid = created.json()["uid"]
        # The owner's view carries it, so the null below is the projection's choice, not a missing
        # column.
        assert owner.get(f"/api/memos/{uid}").json()["idempotency_key"] == KEY

        reader = _login_as(monkeypatch, OTHER)
        one = reader.get(f"/api/memos/{uid}")
        assert one.status_code == 200
        assert one.json()["mine"] is False
        assert "idempotency_key" in one.json() and one.json()["idempotency_key"] is None
        listed = _in_list(reader, uid)
        assert "idempotency_key" in listed and listed["idempotency_key"] is None
        # A reaction answers with the whole memo too, so it is a third road to the same leak.
        reacted = reader.post(f"/api/memos/{uid}/reactions", json={"emoji": "👍"})
        assert reacted.status_code == 200
        assert "idempotency_key" in reacted.json() and reacted.json()["idempotency_key"] is None
    finally:
        _delete_memos_with_key(KEY)
        _drop_user(OTHER)


def test_a_memo_created_without_a_key_carries_null(logged_in_client):
    client, _ = logged_in_client
    created = client.post("/api/memos", json={"content": "unkeyed"})
    assert created.status_code == 201
    uid = created.json()["uid"]
    try:
        assert "idempotency_key" in created.json() and created.json()["idempotency_key"] is None
        one = client.get(f"/api/memos/{uid}")
        assert "idempotency_key" in one.json() and one.json()["idempotency_key"] is None
        listed = _in_list(client, uid)
        assert "idempotency_key" in listed and listed["idempotency_key"] is None
    finally:
        assert client.delete(f"/api/memos/{uid}").status_code in (200, 204)


def test_a_replayed_create_answers_with_the_key_too(logged_in_client):
    # The replay is the answer a retrying outbox actually reads; it must name the create as well.
    client, _ = logged_in_client
    try:
        body = {"content": "retried", "idempotency_key": KEY}
        first, replay = client.post("/api/memos", json=body), client.post("/api/memos", json=body)
        assert first.status_code == replay.status_code == 201
        assert replay.json()["uid"] == first.json()["uid"]
        assert replay.json()["idempotency_key"] == KEY
    finally:
        _delete_memos_with_key(KEY)
