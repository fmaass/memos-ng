"""A PATCH that names a picture list acquires the attachment class ONCE, ascending, up front.

The declared list reaches attachments the memo does not carry yet, and taking those rows after
finalization would be a SECOND acquisition of the attachment class in this transaction, in an order
nothing else shares — which is exactly what the worker's media recovery, taking its terminal
attachments ascending, inverts against. So the list's existing uids are resolved unlocked and go
into the same ONE ascending `FOR UPDATE` that already covers the dedup candidates and the memo's own
attachments, and that holds for a list-only PATCH too: with no uploads there is no finalization to
carry it, and the acquisition has to be asked for on its own.

The probe is a SQL-emission one: `before_cursor_execute` sees every statement in issue order, which
is the only place the ORDER of two acquisitions is observable at all.
"""

import re

import pytest
from app.config import settings
from app.database import engine as async_engine
from sqlalchemy import create_engine, event, text

from tests.conftest import SYNC_URL
from tests.test_memo_picture_list import _memo, _uids, _upload, _wipe

_FOR_UPDATE = re.compile(r"\bFROM attachments\b.*\bFOR UPDATE\b", re.IGNORECASE | re.DOTALL)
_WRITES = re.compile(r"^(UPDATE attachments|INSERT INTO attachments)\b", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _own_data_root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _wipe()
    yield
    _wipe()


def _record_statements() -> tuple[list[tuple[str, object]], object]:
    """Every statement this engine issues, with its parameters, in the order it issued them."""
    seen: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append((" ".join(statement.split()), parameters))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    return seen, record


def _attachment_id(att_uid: str) -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        got = c.execute(
            text("SELECT id FROM attachments WHERE uid = :u"), {"u": att_uid}
        ).scalar_one()
    eng.dispose()
    return got


def _detach(att_uid: str) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("UPDATE attachments SET memo_id = NULL WHERE uid = :u"), {"u": att_uid})
    eng.dispose()


def test_a_list_only_patch_takes_the_named_row_in_its_one_ascending_acquisition(logged_in_client):
    client, _ = logged_in_client
    held = _memo(client, [_upload(client, "held.png", b"\x89PNG\r\n\x1a\n" + b"h" * 2400)])
    held_att = _uids(held)[0]
    loose = _memo(client, [_upload(client, "loose.png", b"\x89PNG\r\n\x1a\n" + b"l" * 2400)])
    loose_att = _uids(loose)[0]
    _detach(loose_att)
    named_id = _attachment_id(loose_att)

    seen, record = _record_statements()
    try:
        r = client.patch(
            f"/api/memos/{held['uid']}",
            json={"attachment_uids": [held_att, loose_att]},  # no upload_uids: nothing finalizes
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)
    assert r.status_code == 200, r.text

    acquisitions = [i for i, (sql, _) in enumerate(seen) if _FOR_UPDATE.search(sql)]
    assert acquisitions, [sql for sql, _ in seen]
    first, params = seen[acquisitions[0]]
    flat = str(params)
    assert str(named_id) in flat, (first, flat)  # the named row is in the FIRST acquisition
    assert "ORDER BY attachments.id" in first, first
    writes = [i for i, (sql, _) in enumerate(seen) if _WRITES.match(sql)]
    assert not writes or writes[0] > acquisitions[0], (
        [seen[i][0] for i in writes[:1]],
        first,
    )
