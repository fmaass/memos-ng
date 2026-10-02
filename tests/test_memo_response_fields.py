"""What a memo response reports after an edit: `total_size`, `updated_at`, and which `visibility`
spelling a write accepts.

All three were questioned by a live probe of PATCH /api/memos/{uid} that sent `attachment_uids`:
the memo held two files yet reported `total_size` 0, `updated_at` looked unmoved (the create and
the edit fell in the same second, and the wire format drops fractions), and a lowercase
`visibility` was refused.
"""

import pytest
from app.config import settings
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_memo_edit_idempotency import _delete_memo, _memo, _upload
from tests.test_uploads import _cleanup

_PNG = b"\x89PNG\r\n\x1a\n"


def _stored_sizes(memo_uid: str) -> dict[str, int]:
    """The authoritative sizes, read from the rows rather than from any response."""
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as c:
            rows = c.execute(
                text(
                    "SELECT a.uid, a.size FROM attachments a JOIN memos m ON a.memo_id = m.id "
                    "WHERE m.uid = :u"
                ),
                {"u": memo_uid},
            ).all()
    finally:
        eng.dispose()
    return {uid: size for uid, size in rows}


def _backdate(memo_uid: str) -> None:
    # Postgres now() is what stamps updated_at, so a frozen Python clock cannot open a gap; moving
    # the committed row an hour back does, and leaves the stamp a whole-second value apart.
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            c.execute(
                text("UPDATE memos SET updated_at = updated_at - interval '1 hour' WHERE uid = :u"),
                {"u": memo_uid},
            )
    finally:
        eng.dispose()


def test_total_size_is_the_sum_of_the_attached_files_on_every_memo_response(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    first = _upload(client, "one.png", _PNG + b"A" * 4000)
    second = _upload(client, "two.png", _PNG + b"B" * 1500)
    created = client.post(
        "/api/memos",
        json={"content": "two files", "visibility": "PRIVATE", "upload_uids": [first, second]},
    )
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]
    try:
        sizes = _stored_sizes(uid)
        assert len(sizes) == 2 and all(s > 0 for s in sizes.values())  # the premise: real sizes
        both = sum(sizes.values())

        assert created.json()["total_size"] == both
        assert client.get(f"/api/memos/{uid}").json()["total_size"] == both
        listed = next(m for m in client.get("/api/memos").json()["items"] if m["uid"] == uid)
        assert listed["total_size"] == both
        assert client.post(f"/api/memos/{uid}/pin").json()["total_size"] == both
        assert client.post(f"/api/memos/{uid}/unpin").json()["total_size"] == both
        reacted = client.post(f"/api/memos/{uid}/reactions", json={"emoji": "👍"})
        assert reacted.json()["total_size"] == both
        assert client.delete(f"/api/memos/{uid}/reactions/👍").json()["total_size"] == both

        # The probe's case: a PATCH naming the attachments by uid. Keeping one of the two must
        # report that one's size — on the PATCH response and on the GET that follows.
        kept = next(a["uid"] for a in created.json()["attachments"])
        patched = client.patch(f"/api/memos/{uid}", json={"attachment_uids": [kept]})
        assert patched.status_code == 200, patched.text
        assert patched.json()["total_size"] == sizes[kept]
        assert client.get(f"/api/memos/{uid}").json()["total_size"] == sizes[kept]
    finally:
        _delete_memo(uid)
        _cleanup()


def test_a_memo_without_files_reports_total_size_zero(logged_in_client):
    client, _ = logged_in_client
    uid = _memo(client, "no files")
    try:
        assert client.get(f"/api/memos/{uid}").json()["total_size"] == 0
    finally:
        _delete_memo(uid)


def test_a_content_patch_advances_updated_at_in_the_response_and_the_following_get(
    logged_in_client,
):
    client, _ = logged_in_client
    uid = _memo(client, "before")
    try:
        _backdate(uid)
        baseline = client.get(f"/api/memos/{uid}").json()["updated_at"]

        patched = client.patch(f"/api/memos/{uid}", json={"content": "after"})
        assert patched.status_code == 200, patched.text
        # Both stamps are the same fixed-width UTC RFC 3339 shape, so string order is time order.
        assert patched.json()["updated_at"] > baseline
        assert client.get(f"/api/memos/{uid}").json()["updated_at"] == patched.json()["updated_at"]
    finally:
        _delete_memo(uid)


# Visibility is the enum spelling on the wire, and the served clients (the web app and the iOS
# client) both send it uppercase. Accepting lowercase would widen the request; nothing needs it, so
# the refusal is the intended behaviour and is held here.
@pytest.mark.parametrize("spelling", ["public", "Public", "private"])
def test_a_visibility_not_in_the_enum_spelling_is_refused_on_create_and_on_patch(
    logged_in_client, spelling
):
    client, _ = logged_in_client
    refused = client.post("/api/memos", json={"content": "x", "visibility": spelling})
    assert refused.status_code == 422, refused.text
    uid = _memo(client, "x")
    try:
        patched = client.patch(f"/api/memos/{uid}", json={"visibility": spelling})
        assert patched.status_code == 422, patched.text
        assert client.get(f"/api/memos/{uid}").json()["visibility"] == "PRIVATE"  # untouched
    finally:
        _delete_memo(uid)


@pytest.mark.parametrize("spelling", ["PUBLIC", "PROTECTED", "PRIVATE"])
def test_the_enum_spelling_is_accepted_on_create_and_on_patch(logged_in_client, spelling):
    client, _ = logged_in_client
    created = client.post("/api/memos", json={"content": "x", "visibility": spelling})
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]
    try:
        assert created.json()["visibility"] == spelling
        patched = client.patch(f"/api/memos/{uid}", json={"visibility": spelling})
        assert patched.status_code == 200 and patched.json()["visibility"] == spelling
    finally:
        _delete_memo(uid)
