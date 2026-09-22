"""One memo save names its WHOLE picture list, in order.

`PATCH /api/memos/{uid}` carries an optional `attachment_uids`. Omitted, the memo's pictures are
untouched and the request stays a true partial update. Present, it is the complete list in display
order: what it names is kept and renumbered, what it omits is taken off the memo, and an entry it
cannot resolve fails the whole request rather than quietly shortening the list — a skipped entry on
a DECLARED list deletes the pictures the list omitted.

An entry is either an attachment uid or an `upload_uid` from the same request, because a client
cannot know the attachment uid of a picture it is uploading in this very call: that uid is minted
inside finalization. Two entries may resolve to one attachment — dedup and print binding both do
that behind the client's back — and the collapse is not an error: the attachment takes the EARLIEST
place its entries asked for.
"""

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog
from app.config import settings
from app.services import reclaim
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_uploads import _drop_user, _login_as, _start

PNG = b"\x89PNG\r\n\x1a\n"


def _bytes(seed: str, size: int = 2400) -> bytes:
    return PNG + (seed.encode() * size)[:size]


def _wipe() -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "TRUNCATE attachment_renditions, media_jobs, attachments, memo_tags, "
                "reactions, memo_edit_keys, memos, uploads RESTART IDENTITY CASCADE"
            )
        )
    eng.dispose()


@pytest.fixture(autouse=True)
def _own_data_root(monkeypatch, tmp_path):
    """Every probe here writes real files and reads them back, so it gets its own DATA_ROOT and a
    table it did not inherit."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _wipe()
    yield
    _wipe()


def _upload(client, name: str, data: bytes, mime: str = "image/png") -> str:
    up = _start(client, name, mime, len(data))
    r = client.patch(f"/api/uploads/{up}", content=data, headers={"Upload-Offset": "0"})
    assert r.status_code in (200, 204), r.text
    return up


def _memo(client, uploads: list[str] | None = None, content: str = "x") -> dict:
    body: dict = {"content": content, "visibility": "PUBLIC"}
    if uploads:
        body["upload_uids"] = uploads
    r = client.post("/api/memos", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _uids(memo: dict) -> list[str]:
    return [a["uid"] for a in memo["attachments"]]


def _get(client, memo_uid: str) -> dict:
    r = client.get(f"/api/memos/{memo_uid}")
    assert r.status_code == 200, r.text
    return r.json()


def _positions(memo_uid: str) -> list[tuple[str, int | None]]:
    """(attachment uid, position) for the memo, read straight from the table in DISPLAY order.

    The response carries the relation's order; only the column says whether that order is a stored
    fact or an accident of insertion ids.
    """
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT a.uid, a.position FROM attachments a JOIN memos m ON m.id = a.memo_id"
                " WHERE m.uid = :u ORDER BY a.position NULLS LAST, a.id"
            ),
            {"u": memo_uid},
        ).all()
    eng.dispose()
    return [(u, p) for u, p in rows]


def _attachment_rows() -> dict[str, tuple[int | None, object]]:
    """uid -> (memo_id, detached_at) for every attachment row that still exists."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(text("SELECT uid, memo_id, detached_at FROM attachments")).all()
    eng.dispose()
    return {u: (m, d) for u, m, d in rows}


def _storage_path(att_uid: str) -> str:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        path = c.execute(
            text("SELECT storage_path FROM attachments WHERE uid = :u"), {"u": att_uid}
        ).scalar_one()
    eng.dispose()
    return path


def _rendition_keys(att_uid: str) -> list[str]:
    """The rendition masters hanging off an attachment — where an IN-BATCH print lands, as opposed
    to the sibling bind, which fills the attachment's own print_path."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT r.variant_key FROM attachment_renditions r"
                " JOIN attachments a ON a.id = r.attachment_id WHERE a.uid = :u"
            ),
            {"u": att_uid},
        ).all()
    eng.dispose()
    return [k for (k,) in rows]


def _print_path(att_uid: str) -> str | None:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        path = c.execute(
            text("SELECT print_path FROM attachments WHERE uid = :u"), {"u": att_uid}
        ).scalar_one()
    eng.dispose()
    return path


def _refusals(response) -> list[tuple[str, str]]:
    """(offending uid, reason code) per rejected entry, off the response BODY.

    The status alone cannot tell a client which picture it lost, which is the whole reason this
    refusal is typed.
    """
    body = response.json()["detail"]
    out = []
    for problem in body:
        assert problem["loc"][:2] == ["body", "attachment_uids"], problem
        assert problem["msg"], problem
        assert problem["type"].startswith("attachment_uid."), problem
        out.append((problem["loc"][2], problem["type"].removeprefix("attachment_uid.")))
    return out


def _three(client) -> tuple[dict, list[str]]:
    """A memo carrying three pictures, in the order they were sent."""
    ups = [_upload(client, f"{n}.png", _bytes(n)) for n in ("alpha", "bravo", "charlie")]
    memo = _memo(client, ups)
    uids = _uids(memo)
    assert len(uids) == 3
    return memo, uids


@contextmanager
def _unlink_always_fails() -> Iterator[list[str]]:
    """Hold every reclaim at its unlink seam, where it fails closed: it rolls its own transaction
    back and leaves the row detached with its files intact. Yields the paths it was asked for."""
    asked: list[str] = []

    def always_fails(paths: list[Path]) -> list[str]:
        asked.extend(str(path) for path in paths)
        return [str(path) for path in paths]

    original = reclaim.unlink_paths
    reclaim.unlink_paths = always_fails
    try:
        yield asked
    finally:
        reclaim.unlink_paths = original


# ---- the list is the whole list -------------------------------------------------------------


def test_the_list_declares_order_and_what_it_omits_leaves_the_memo(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [c, a]})

    assert r.status_code == 200, r.text
    assert _uids(r.json()) == [c, a]
    assert _uids(_get(client, memo["uid"])) == [c, a]
    assert b not in _uids(_get(client, memo["uid"]))


def test_every_kept_attachment_carries_its_place(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [c, b, a]})

    assert r.status_code == 200, r.text
    assert _positions(memo["uid"]) == [(c, 0), (b, 1), (a, 2)]


def test_an_empty_list_removes_every_picture(logged_in_client):
    client, _ = logged_in_client
    memo, uids = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": []})

    assert r.status_code == 200, r.text
    assert _uids(r.json()) == []
    assert _uids(_get(client, memo["uid"])) == []
    assert not set(uids) & set(_attachment_rows())


def test_omitting_the_field_removes_none(logged_in_client):
    client, _ = logged_in_client
    memo, uids = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"content": "still three"})

    assert r.status_code == 200, r.text
    assert _uids(r.json()) == uids
    assert r.json()["content"] == "still three"


def test_one_call_adds_removes_and_reorders(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    fresh = _upload(client, "delta.png", _bytes("delta"))

    r = client.patch(
        f"/api/memos/{memo['uid']}",
        json={"upload_uids": [fresh], "attachment_uids": [fresh, c, a]},
    )

    assert r.status_code == 200, r.text
    got = _uids(r.json())
    assert len(got) == 3
    assert got[1:] == [c, a]
    assert got[0] not in (a, b, c)  # the picture this same call uploaded, first in the list
    assert b not in got
    assert [p for _, p in _positions(memo["uid"])] == [0, 1, 2]


# ---- a replayed edit deletes nothing further --------------------------------------------------


def test_a_replayed_edit_deletes_nothing_further(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    key = str(uuid.uuid4())
    body = {"attachment_uids": [a], "edit_key": key}

    first = client.patch(f"/api/memos/{memo['uid']}", json=body)
    assert first.status_code == 200, first.text
    after_first = len(_attachment_rows())

    second = client.patch(f"/api/memos/{memo['uid']}", json=body)

    assert second.status_code == 200, second.text
    assert _uids(second.json()) == [a]
    assert len(_attachment_rows()) == after_first


# ---- an entry that cannot be resolved is refused, never skipped -------------------------------


def test_a_uid_that_names_nothing_is_refused(logged_in_client):
    client, _ = logged_in_client
    memo, uids = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [uids[0], "no-such"]})

    assert r.status_code == 422, r.text
    assert _refusals(r) == [("no-such", "missing")]
    assert _uids(_get(client, memo["uid"])) == uids  # the whole request failed


def test_another_owners_uid_is_refused_and_neither_memo_moves(logged_in_client, monkeypatch):
    client, _ = logged_in_client
    memo, uids = _three(client)
    stranger = _login_as(monkeypatch, "picture_list_stranger")
    try:
        theirs = _memo(stranger, [_upload(stranger, "theirs.png", _bytes("theirs"))])
        their_uid = _uids(theirs)[0]

        r = client.patch(
            f"/api/memos/{memo['uid']}", json={"attachment_uids": [uids[0], their_uid]}
        )

        assert r.status_code == 422, r.text
        assert _refusals(r) == [(their_uid, "foreign_owner")]
        assert _uids(_get(client, memo["uid"])) == uids
        assert _uids(_get(stranger, theirs["uid"])) == [their_uid]
    finally:
        _drop_user("picture_list_stranger")


def test_a_uid_on_a_different_memo_is_refused_and_neither_memo_moves(logged_in_client):
    client, _ = logged_in_client
    mine, uids = _three(client)
    other = _memo(client, [_upload(client, "other.png", _bytes("other"))])
    other_uid = _uids(other)[0]

    r = client.patch(f"/api/memos/{mine['uid']}", json={"attachment_uids": [uids[0], other_uid]})

    assert r.status_code == 422, r.text
    assert _refusals(r) == [(other_uid, "other_memo")]
    assert _uids(_get(client, mine["uid"])) == uids
    assert _uids(_get(client, other["uid"])) == [other_uid]


def test_an_upload_uid_this_request_did_not_name_is_refused(logged_in_client):
    client, _ = logged_in_client
    memo, uids = _three(client)
    stranded = _upload(client, "stranded.png", _bytes("stranded"))

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [uids[0], stranded]})

    assert r.status_code == 422, r.text
    assert _refusals(r) == [(stranded, "unresolvable_upload_uid")]
    assert _uids(_get(client, memo["uid"])) == uids


def test_every_rejected_entry_is_named_at_once(logged_in_client):
    client, _ = logged_in_client
    memo, uids = _three(client)

    r = client.patch(
        f"/api/memos/{memo['uid']}", json={"attachment_uids": ["nope-a", uids[0], "nope-b"]}
    )

    assert r.status_code == 422, r.text
    assert _refusals(r) == [("nope-a", "missing"), ("nope-b", "missing")]


# ---- an upload uid resolves to the attachment it produced -------------------------------------


def test_an_upload_uid_names_the_attachment_it_produced(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    fresh = _upload(client, "echo.png", _bytes("echo"))

    r = client.patch(
        f"/api/memos/{memo['uid']}", json={"upload_uids": [fresh], "attachment_uids": [b, fresh]}
    )

    assert r.status_code == 200, r.text
    got = _uids(r.json())
    assert len(got) == 2
    assert got[0] == b
    assert got[1] not in (a, b, c)
    assert _positions(memo["uid"]) == [(b, 0), (got[1], 1)]


def test_the_same_upload_uid_twice_takes_one_place(logged_in_client):
    client, _ = logged_in_client
    # The memo starts with pictures the list will NOT name, so "one place" cannot be satisfied by
    # a request that simply appended its upload and left the rest alone.
    memo, uids = _three(client)
    fresh = _upload(client, "foxtrot.png", _bytes("foxtrot"))

    r = client.patch(
        f"/api/memos/{memo['uid']}",
        json={"upload_uids": [fresh], "attachment_uids": [fresh, fresh]},
    )

    assert r.status_code == 200, r.text
    got = _uids(r.json())
    assert len(got) == 1
    assert got[0] not in uids
    assert [p for _, p in _positions(memo["uid"])] == [0]


def test_two_uploads_of_identical_bytes_collapse_to_the_earlier_place(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    same = _bytes("twin")
    assert hashlib.sha256(same).hexdigest()  # one digest, two upload sessions
    first = _upload(client, "twin-one.png", same)
    second = _upload(client, "twin-two.png", same)

    r = client.patch(
        f"/api/memos/{memo['uid']}",
        json={"upload_uids": [first, second], "attachment_uids": [a, second, b, first]},
    )

    assert r.status_code == 200, r.text
    got = _uids(r.json())
    # Four entries, three pictures: the twin takes the EARLIER of the two places that named it.
    assert len(got) == 3
    assert got[0] == a
    assert got[2] == b
    assert got[1] not in (a, b, c)
    assert _positions(memo["uid"]) == [(got[0], 0), (got[1], 1), (got[2], 2)]


def test_an_in_batch_print_names_its_screen(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    screen = _upload(client, "Golden Hour.png", _bytes("golden"))
    glossy = _upload(client, "Golden Hour - print (glossy).png", _bytes("golden-print", 3000))

    r = client.patch(
        f"/api/memos/{memo['uid']}",
        json={"upload_uids": [screen, glossy], "attachment_uids": [glossy, screen, a]},
    )

    assert r.status_code == 200, r.text
    got = _uids(r.json())
    # The print never becomes an attachment of its own: it binds onto its screen, so both entries
    # name that one attachment, at the earlier of their two places.
    assert len(got) == 2
    assert got[1] == a
    assert got[0] not in (a, b, c)
    assert any(k.startswith("print") for k in _rendition_keys(got[0]))


def test_a_memo_sibling_print_names_the_sibling(logged_in_client):
    client, _ = logged_in_client
    screen_up = _upload(client, "Harbour.png", _bytes("harbour"))
    # A second picture the list will not name, so the assertion below cannot be satisfied by a
    # request that bound the print and left the memo's pictures where they were.
    spare_up = _upload(client, "Lighthouse.png", _bytes("lighthouse"))
    memo = _memo(client, [screen_up, spare_up])
    screen_att, spare_att = _uids(memo)
    glossy = _upload(client, "Harbour - print (glossy).png", _bytes("harbour-print", 3000))

    r = client.patch(
        f"/api/memos/{memo['uid']}",
        json={"upload_uids": [glossy], "attachment_uids": [glossy]},
    )

    assert r.status_code == 200, r.text
    # Naming only the print must not take the screen off the memo: the print IS that screen's
    # picture, and the list named it by the only uid the client could have.
    assert _uids(r.json()) == [screen_att]
    assert _print_path(screen_att) is not None
    assert spare_att not in _attachment_rows()


def test_naming_a_print_and_its_sibling_collapses_to_one_place(logged_in_client):
    client, _ = logged_in_client
    screen_up = _upload(client, "Jetty.png", _bytes("jetty"))
    other_up = _upload(client, "Kiosk.png", _bytes("kiosk"))
    memo = _memo(client, [screen_up, other_up])
    screen_att, other_att = _uids(memo)
    glossy = _upload(client, "Jetty - print (glossy).png", _bytes("jetty-print", 3000))

    r = client.patch(
        f"/api/memos/{memo['uid']}",
        json={"upload_uids": [glossy], "attachment_uids": [other_att, glossy, screen_att]},
    )

    assert r.status_code == 200, r.text
    assert _uids(r.json()) == [other_att, screen_att]
    assert _positions(memo["uid"]) == [(other_att, 0), (screen_att, 1)]


# ---- a named row whose bytes are gone is refused (19d, through the route) ---------------------


def _strip_original(att_uid: str) -> None:
    """Take the original file out from under a row that still names it — the shape a removal that
    rolled back after its first unlink leaves behind."""
    from pathlib import Path

    Path(_storage_path(att_uid)).unlink()


def test_a_stamped_detached_row_whose_original_is_gone_is_refused(logged_in_client):
    client, _ = logged_in_client
    donor = _memo(client, [_upload(client, "donor.png", _bytes("donor"))])
    orphan = _uids(donor)[0]
    with _unlink_always_fails():
        assert client.patch(f"/api/memos/{donor['uid']}", json={"attachment_uids": []}).status_code
    assert _attachment_rows()[orphan][1] is not None  # detached, and the instant is recorded
    _strip_original(orphan)
    memo, uids = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [uids[0], orphan]})

    assert r.status_code == 422, r.text
    assert _refusals(r) == [(orphan, "missing")]
    assert _uids(_get(client, memo["uid"])) == uids


def test_an_unstamped_damaged_orphan_is_refused(logged_in_client):
    client, _ = logged_in_client
    donor = _memo(client, [_upload(client, "damaged.png", _bytes("damaged"))])
    orphan = _uids(donor)[0]
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        # An orphan from before the detach instant existed: nothing recorded when it was taken off
        # its memo, so nothing but its own files can answer for it.
        c.execute(
            text("UPDATE attachments SET memo_id = NULL, detached_at = NULL WHERE uid = :u"),
            {"u": orphan},
        )
    eng.dispose()
    _strip_original(orphan)
    memo, uids = _three(client)

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [uids[0], orphan]})

    assert r.status_code == 422, r.text
    assert _refusals(r) == [(orphan, "missing")]
    assert _uids(_get(client, memo["uid"])) == uids


# ---- the reclaim runs after the commit and cannot take the save down with it ------------------


def test_a_failed_reclaim_leaves_the_save_committed_and_says_so(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)

    with _unlink_always_fails(), structlog.testing.capture_logs() as logs:
        r = client.patch(
            f"/api/memos/{memo['uid']}",
            json={"content": "renamed", "attachment_uids": [c, a]},
        )

    assert r.status_code == 200, r.text
    # A file this request could not delete must not undo an edit the user was told was saved, so
    # everything the save declared is here: the content, the order, and the detach.
    assert r.json()["content"] == "renamed"
    assert _uids(_get(client, memo["uid"])) == [c, a]
    assert _positions(memo["uid"]) == [(c, 0), (a, 1)]
    assert _attachment_rows()[b][0] is None
    said = [entry for entry in logs if entry["event"] == "memo.patch.attachment_not_reclaimed"]
    # Both uids, because the line is what points a hand at the row before the grace elapses.
    assert [(e["log_level"], e["memo_uid"], e["attachment_uid"]) for e in said] == [
        ("warning", memo["uid"], b)
    ]


def test_a_removed_pictures_row_and_files_are_gone_once_the_request_returns(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    original = Path(_storage_path(b))
    assert original.exists()

    r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [c, a]})

    assert r.status_code == 200, r.text
    assert b not in _attachment_rows()
    assert not original.exists()


def test_between_the_commit_and_the_unlink_the_row_is_merely_detached(logged_in_client):
    client, _ = logged_in_client
    memo, (a, b, c) = _three(client)
    original = Path(_storage_path(b))

    with _unlink_always_fails() as asked:
        r = client.patch(f"/api/memos/{memo['uid']}", json={"attachment_uids": [c, a]})

    assert r.status_code == 200, r.text
    # The reclaim got as far as its files and no further, so what stands is what the save left.
    assert str(original) in asked
    memo_id, detached_at = _attachment_rows()[b]
    assert memo_id is None
    assert detached_at is not None
    assert b not in _uids(_get(client, memo["uid"]))
    assert original.exists()
