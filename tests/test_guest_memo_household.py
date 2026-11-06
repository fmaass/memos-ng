"""A guest memo is the household's to moderate, and nobody adds to it (MEMNG-1264).

A guest batch becomes a memo OWNED by the pass issuer only because guests have no account here
(`passes.create_guest_memo`). So the edit rule is not "the owner": any signed-in household member
may edit a guest memo, delete it, and take its pictures off it — a member memo stays owner-only.
And no one, issuer included, adds a picture to a guest memo: that is the guest's batch, and the
apps offer no Add on it. Every case goes over the wire with two real sessions.
"""

import pytest
from app.csrf import canonical_origin
from app.main import app
from fastapi.testclient import TestClient

from tests.test_attachments_taken import _sql
from tests.test_collections_api import _att
from tests.test_memo_guest import _guest, _member
from tests.test_passes_api import _trip, lib, two  # noqa: F401  (fixtures)


def _attachments(memo_uid: str) -> list[str]:
    return [
        r[0]
        for r in _sql(
            "SELECT a.uid FROM attachments a JOIN memos m ON m.id = a.memo_id "
            "WHERE m.uid = :u ORDER BY a.position NULLS LAST, a.id",
            {"u": memo_uid},
        )
    ]


def _exists(memo_uid: str) -> bool:
    return bool(_sql("SELECT 1 FROM memos WHERE uid = :u", {"u": memo_uid}))


async def _pass_memo(client, owner: str, *pictures: str) -> str:
    for uid in pictures:
        _att(uid, owner)
    memo_uid, _ = await _guest(_trip(client), owner, list(pictures), name="Anna")
    return memo_uid


@pytest.fixture
def anonymous():
    c = TestClient(app, raise_server_exceptions=False)
    c.headers["Origin"] = canonical_origin()
    return c


async def test_the_issuer_edits_and_deletes_a_guest_memo(two):  # noqa: F811
    a_client, a, _, _ = two
    memo_uid = await _pass_memo(a_client, a, "hi1")
    r = a_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True})
    assert r.status_code == 200, r.text
    assert r.json()["mine"] is True
    r = a_client.delete(f"/api/memos/{memo_uid}")
    assert r.status_code == 204, r.text
    assert not _exists(memo_uid)


async def test_another_member_edits_a_guest_memo_and_takes_a_picture_off_it(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "ho1", "ho2", "ho3")
    r = b_client.patch(f"/api/memos/{memo_uid}", json={"content": "#tp1trip moderated"})
    assert r.status_code == 200, r.text
    assert r.json()["mine"] is False  # rendered for the member who asked, not for the issuer
    assert _sql("SELECT content FROM memos WHERE uid = :u", {"u": memo_uid}) == [
        ("#tp1trip moderated",)
    ]
    # The picture list without one of them: the edit sheet's per-picture Remove.
    r = b_client.patch(f"/api/memos/{memo_uid}", json={"attachment_uids": ["ho1", "ho3"]})
    assert r.status_code == 200, r.text
    assert _attachments(memo_uid) == ["ho1", "ho3"]
    # And the attachment route, which deletes the file outright.
    r = b_client.delete("/api/attachments/ho3")
    assert r.status_code == 204, r.text
    assert _sql("SELECT count(*) FROM attachments WHERE uid = 'ho3'") == [(0,)]
    assert _attachments(memo_uid) == ["ho1"]


async def test_another_member_deletes_a_guest_memo(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "hd1")
    r = b_client.delete(f"/api/memos/{memo_uid}")
    assert r.status_code == 204, r.text
    assert not _exists(memo_uid)
    assert a_client.get(f"/api/memos/{memo_uid}").status_code == 404


async def test_a_member_memo_stays_its_owners_alone(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = _member(a_client)
    [(memo_id,)] = _sql("SELECT id FROM memos WHERE uid = :u", {"u": memo_uid})
    _att("hm1", a, memo_id)
    assert a_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True}).status_code == 200
    assert b_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True}).status_code == 403
    assert b_client.delete(f"/api/memos/{memo_uid}").status_code == 403
    assert b_client.delete("/api/attachments/hm1").status_code == 404
    assert _exists(memo_uid)
    assert _attachments(memo_uid) == ["hm1"]


async def test_nobody_signed_out_touches_a_guest_memo(two, anonymous):  # noqa: F811
    a_client, a, _, _ = two
    memo_uid = await _pass_memo(a_client, a, "hu1")
    assert anonymous.patch(f"/api/memos/{memo_uid}", json={"pinned": True}).status_code == 401
    assert anonymous.delete(f"/api/memos/{memo_uid}").status_code == 401
    assert anonymous.delete("/api/attachments/hu1").status_code == 401
    assert _exists(memo_uid)
    assert _attachments(memo_uid) == ["hu1"]


@pytest.mark.parametrize("who", ["issuer", "member"])
async def test_nobody_adds_a_picture_to_a_guest_memo(two, who):  # noqa: F811
    a_client, a, b_client, _ = two
    client = a_client if who == "issuer" else b_client
    memo_uid = await _pass_memo(a_client, a, "hn1")
    _att("hn2", a)
    r = client.patch(f"/api/memos/{memo_uid}", json={"attachment_uids": ["hn1", "hn2"]})
    assert r.status_code == 409, r.text
    assert "guest memo takes no new pictures" in r.json()["detail"]
    r = client.patch(f"/api/memos/{memo_uid}", json={"upload_uids": ["any-upload"]})
    assert r.status_code == 409, r.text
    assert "guest memo takes no new pictures" in r.json()["detail"]
    assert _attachments(memo_uid) == ["hn1"]
    assert _sql("SELECT memo_id FROM attachments WHERE uid = 'hn2'") == [(None,)]


async def test_an_empty_list_or_no_uploads_adds_nothing_and_is_not_refused(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "he1", "he2")
    r = b_client.patch(f"/api/memos/{memo_uid}", json={"upload_uids": [], "pinned": True})
    assert r.status_code == 200, r.text
    r = b_client.patch(f"/api/memos/{memo_uid}", json={"attachment_uids": []})
    assert r.status_code == 200, r.text
    assert _attachments(memo_uid) == []


async def test_a_member_memo_carrying_the_trip_tag_is_still_its_owners(two):  # noqa: F811
    a_client, a, b_client, _ = two
    await _pass_memo(a_client, a, "ht1")
    r = a_client.post("/api/memos", json={"content": "#tp1trip", "visibility": "PROTECTED"})
    assert r.status_code == 201, r.text
    memo_uid = r.json()["uid"]
    assert b_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True}).status_code == 403
    assert b_client.delete(f"/api/memos/{memo_uid}").status_code == 403


async def test_a_refused_addition_leaves_the_edit_key_free(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "hk1")
    _att("hk2", a)
    key = "11111111-2222-4333-8444-555555555555"
    r = b_client.patch(
        f"/api/memos/{memo_uid}", json={"attachment_uids": ["hk1", "hk2"], "edit_key": key}
    )
    assert r.status_code == 409, r.text
    assert _sql("SELECT count(*) FROM memo_edit_keys") == [(0,)]
    r = b_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True, "edit_key": key})
    assert r.status_code == 200, r.text


async def test_the_rule_reaches_edit_and_delete_only_not_pinning(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "hp1")
    assert b_client.post(f"/api/memos/{memo_uid}/pin").status_code == 403


async def test_once_the_pass_record_is_gone_the_memo_is_its_owners_again(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "hg1")
    for table in ("pass_sync_state", "pass_events", "pass_devices", "passes"):
        _sql(f"DELETE FROM {table}")
    assert b_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True}).status_code == 403
    assert b_client.delete("/api/attachments/hg1").status_code == 404
    assert b_client.delete(f"/api/memos/{memo_uid}").status_code == 403
    assert _attachments(memo_uid) == ["hg1"]


async def test_an_edit_key_conflict_never_names_a_memo_the_member_cannot_see(two):  # noqa: F811
    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "hx1")
    r = a_client.post("/api/memos", json={"content": "diary", "visibility": "PRIVATE"})
    assert r.status_code == 201, r.text
    private_uid = r.json()["uid"]
    key = "99999999-2222-4333-8444-555555555555"
    r = a_client.patch(f"/api/memos/{private_uid}", json={"pinned": True, "edit_key": key})
    assert r.status_code == 200, r.text
    # The member's edit of the guest memo lives in the issuer's key namespace: the clash is real
    # and refused, but the issuer's private memo is not the member's to learn about.
    r = b_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True, "edit_key": key})
    assert r.status_code == 409, r.text
    assert private_uid not in r.text
    assert r.json()["detail"]["code"] == "edit_key_conflict"
    # The issuer still hears which memo holds the key.
    r = a_client.patch(f"/api/memos/{memo_uid}", json={"pinned": True, "edit_key": key})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["memo_uid"] == private_uid


async def test_a_picture_moved_off_the_guest_memo_before_the_reclaim_is_not_a_members_to_delete(
    two,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    """The member was let in through the guest memo; if the picture is on the owner's own memo by
    the time its row is locked, that permission is gone and the delete must not land."""
    from app.services import reclaim

    a_client, a, b_client, _ = two
    memo_uid = await _pass_memo(a_client, a, "hr1")
    own_uid = _member(a_client)
    [(own_id,)] = _sql("SELECT id FROM memos WHERE uid = :u", {"u": own_uid})
    original = tmp_path / "hr1.jpg"
    original.write_bytes(b"\xff\xd8 still here")
    _sql("UPDATE attachments SET storage_path = :p WHERE uid = 'hr1'", {"p": str(original)})
    real = reclaim.reclaim_row_shielded

    async def moved_first(*args, **kwargs):
        # Between the route's check and the reclaim's lock: the owner files it elsewhere.
        _sql("UPDATE attachments SET memo_id = :m WHERE uid = 'hr1'", {"m": own_id})
        return await real(*args, **kwargs)

    monkeypatch.setattr(reclaim, "reclaim_row_shielded", moved_first)
    r = b_client.delete("/api/attachments/hr1")
    assert r.status_code == 404, r.text
    assert _attachments(own_uid) == ["hr1"]
    assert original.read_bytes() == b"\xff\xd8 still here"
    assert _attachments(memo_uid) == []
