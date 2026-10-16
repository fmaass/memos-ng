"""MEMNG-163 — a cursor this server did not mint is a 422, never a 500 and never a silent
restart from page 1 (which makes a paging client loop over the first page for ever).

One case per INPUT CATEGORY, not just the happy path: malformed encoding, non-canonical base64
that the permissive decoder used to accept as a valid row id, a structurally wrong payload that
IS valid base64, a truncated real token, and the valid token that must keep working.
"""

import base64
from datetime import UTC, datetime

import pytest
from app.models import Attachment, Memo
from app.routers.assets import decode_attachment_cursor, encode_attachment_cursor
from app.services.cursors import InvalidCursor, decode_cursor_payload, encode_cursor_payload
from app.services.memos import decode_cursor, encode_cursor

# --- categories, named so a failure says which class of junk got through ---

MALFORMED_ENCODING = [
    "not base64 at all!",  # characters outside the alphabet
    "%%%",  # url-ish junk
    "M!Q==",  # the permissive decoder DISCARDED the "!" and read this as id 1
    "MQ",  # padding stripped — not a token this server mints
    "MQ===",  # over-padded
    "MQ==\n",  # trailing newline the permissive decoder ignored
    "MüQ==",  # non-ascii
    "",  # empty: a bad token, not "start from the top"
]

# Valid base64url whose PAYLOAD is not a keyset token.
STRUCTURALLY_WRONG = [
    encode_cursor_payload("foo"),  # no separator at all
    encode_cursor_payload("|7"),  # empty timestamp
    encode_cursor_payload("not-a-timestamp|7"),
    encode_cursor_payload("2026-01-02T03:04:05+00:00|not-an-id"),
    base64.urlsafe_b64encode(b"\xff\xfe").decode(),  # decodes, but not to text
]


# A real token with its tail clipped off the base64 quantum boundary.
TRUNCATED = encode_cursor_payload("2026-01-02T03:04:05+00:00|4711")[:-1]


def _a_real_memo_cursor() -> str:
    return encode_cursor(Memo(id=4711, created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)))


# --- the decoders themselves ---


def test_a_minted_memo_cursor_round_trips():
    cursor = _a_real_memo_cursor()
    when, mid = decode_cursor(cursor)
    assert mid == 4711
    assert when == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_a_minted_attachment_cursor_round_trips():
    assert decode_attachment_cursor(encode_attachment_cursor(Attachment(id=4711))) == 4711


@pytest.mark.parametrize("bad", MALFORMED_ENCODING)
def test_malformed_encoding_is_refused_by_every_decoder(bad):
    for decode in (decode_cursor_payload, decode_cursor, decode_attachment_cursor):
        with pytest.raises(InvalidCursor):
            decode(bad)


@pytest.mark.parametrize("bad", STRUCTURALLY_WRONG)
def test_a_wrong_shaped_payload_is_refused(bad):
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_a_truncated_memo_cursor_never_raises_through():
    """Truncation is the field shape of this bug — a clipped token in a link or a log replay.

    Every prefix must land as `InvalidCursor` or as a decoded-but-different cursor, never as a
    raw ValueError the API layer has no answer for: that raise-through IS the 500 MEMNG-163
    filed. A prefix that happens to fall on a base64 quantum boundary can still be a well-formed
    keyset token for a smaller id — only a signed cursor could tell those apart, and a cursor is
    an offset, not a capability.
    """
    cursor = _a_real_memo_cursor()
    full = decode_cursor(cursor)
    for keep in range(1, len(cursor)):
        try:
            assert decode_cursor(cursor[:keep]) != full
        except InvalidCursor:
            pass


def test_an_attachment_cursor_is_not_a_memo_cursor():
    """The two lists mint different payloads; neither may be read as the other's."""
    with pytest.raises(InvalidCursor):
        decode_cursor(encode_attachment_cursor(Attachment(id=4711)))
    with pytest.raises(InvalidCursor):
        decode_attachment_cursor(_a_real_memo_cursor())


# --- what the client actually sees ---


@pytest.mark.parametrize("bad", MALFORMED_ENCODING + STRUCTURALLY_WRONG + [TRUNCATED])
def test_a_bad_cursor_on_the_memo_feed_is_422(logged_in_client, bad):
    client, _ = logged_in_client
    r = client.get("/api/memos", params={"cursor": bad})
    assert r.status_code == 422, f"{bad!r} → {r.status_code}"
    assert r.json()["detail"] == "Invalid cursor"


@pytest.mark.parametrize("bad", ["not base64 at all!", "M!Q==", encode_cursor_payload("foo")])
def test_a_bad_pagetoken_on_the_compat_feed_is_422(logged_in_client, bad):
    """/api/v1/memos decodes the same keyset payload — the satellites' feed must not 500 either."""
    client, _ = logged_in_client
    r = client.get("/api/v1/memos", params={"pageToken": bad})
    assert r.status_code == 422, f"{bad!r} → {r.status_code}"


def test_a_valid_cursor_still_pages_the_memo_feed(logged_in_client):
    """The positive control for the tests above: the SAME endpoint, a token the server minted."""
    client, _ = logged_in_client
    made = [
        client.post("/api/memos", json={"content": f"page me {i}", "visibility": "PRIVATE"})
        for i in range(3)
    ]
    uids = [r.json()["uid"] for r in made]
    try:
        first = client.get("/api/memos", params={"limit": 2})
        assert first.status_code == 200
        cursor = first.json()["nextCursor"]
        assert cursor
        second = client.get("/api/memos", params={"limit": 2, "cursor": cursor})
        assert second.status_code == 200
        seen = [m["uid"] for m in first.json()["items"]] + [
            m["uid"] for m in second.json()["items"]
        ]
        assert set(uids) <= set(seen)
        assert len(seen) == len(set(seen)), "a page was served twice"
    finally:
        for uid in uids:
            client.delete(f"/api/memos/{uid}")
