"""The capture read, the inspection endpoint, and the saved attachment order (MEMNG-262).

Every expectation here comes out of `fixtures/auto-memo-date.json` — the ONE normative artefact
the SPA's vitest suite and the iOS client are held to as well. Nothing restates a rule locally:
a vector changes in the artefact or it does not change.
"""

import builtins
import hashlib
import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.config import settings
from app.csrf import canonical_origin
from app.main import app
from app.services import capture
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

ROOT = Path(__file__).resolve().parent.parent
ARTEFACT = json.loads((ROOT / "fixtures" / "auto-memo-date.json").read_text())
BINARIES = ROOT / "fixtures" / "auto-memo-date"


def _instant(value: datetime | None) -> str | None:
    """Render for comparison — after asserting the precision on the RAW value.

    `strftime` drops microseconds by itself, so a formatted comparison passes even if production
    stopped flooring. The assertion below is what actually holds "whole seconds everywhere".
    """
    if value is None:
        return None
    assert value.microsecond == 0, f"{value!r} carries sub-second precision"
    assert value.tzinfo is not None, f"{value!r} is naive"
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expect(name: str) -> dict[str, str | None]:
    """What the artefact says this binary fixture reads as. The ONE source of that answer."""
    return ARTEFACT["binaries"][name]["expect"]


def _expected_instant(name: str) -> datetime:
    raw = _expect(name)["capture_instant"]
    assert isinstance(raw, str)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachment_renditions"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
        c.execute(text("DELETE FROM memos"))
    eng.dispose()


def _start(client, name, mime, size):
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": size, "last_modified": 1609459200000},
    )
    assert r.status_code == 201, r.text
    return r.json()["upload_uid"]


def _upload_bytes(client, name, mime, data: bytes) -> str:
    uid = _start(client, name, mime, len(data))
    r = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert r.status_code == 200, r.text
    return uid


def _upload_fixture(client, name: str) -> str:
    spec = ARTEFACT["binaries"][name]
    return _upload_bytes(client, name, spec["mime"], (BINARIES / name).read_bytes())


def _png(marker: bytes) -> bytes:
    """Distinct bytes per call — the sha256 dedup would otherwise fold two tiles into one row."""
    return b"\x89PNG\r\n\x1a\n" + marker + b"0" * 3000


def _login_as(monkeypatch, subject):
    """A SECOND authenticated session (the conftest fixture is pinned to one subject)."""
    from unittest.mock import AsyncMock

    from app import auth

    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Origin"] = canonical_origin()
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth,
        "validate_id_token",
        lambda t: {"preferred_username": subject, "name": "O", "email": "o@x", "groups": []},
    )
    login = client.get("/api/auth/login", follow_redirects=False)
    tx = login.cookies[auth.TX_COOKIE]
    state = auth.decrypt(tx)["state"]
    client.cookies.set(auth.TX_COOKIE, tx)
    cb = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    client.cookies.set(auth.SID_COOKIE, cb.cookies[auth.SID_COOKIE])
    return client


def _drop_user(subject):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
    eng.dispose()


def _filenames_in_order(memo_uid: str) -> list[str]:
    """The memo's attachment FILENAMES in saved order — what a place actually holds."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as conn:
        names = list(
            conn.execute(
                text(
                    "SELECT a.filename FROM attachments a JOIN memos m ON m.id = a.memo_id "
                    "WHERE m.uid = :u ORDER BY a.position NULLS LAST, a.id"
                ),
                {"u": memo_uid},
            ).scalars()
        )
    eng.dispose()
    return names


def _memo_attachments(client, uid: str) -> list[dict]:
    r = client.get(f"/api/memos/{uid}")
    assert r.status_code == 200, r.text
    return r.json()["attachments"]


def _set_positions(memo_uid: str, by_index: dict[int, int | None]) -> list[int]:
    """Force the stored positions of a memo's attachments (by their id order) — the only way to
    manufacture the LEGACY shapes: rows written before the column existed, and the mixture a
    PATCH can leave behind. Returns the attachment ids in id order."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        ids = list(
            c.execute(
                text(
                    "SELECT a.id FROM attachments a JOIN memos m ON m.id = a.memo_id "
                    "WHERE m.uid = :u ORDER BY a.id"
                ),
                {"u": memo_uid},
            ).scalars()
        )
        for i, pos in by_index.items():
            c.execute(
                text("UPDATE attachments SET position = :p WHERE id = :i"),
                {"p": pos, "i": ids[i]},
            )
    eng.dispose()
    return ids


# --------------------------------------------------------------------------------------------
# The pipeline itself, against the artefact's vectors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ARTEFACT["binaries"]))
def test_binary_fixture_reads_exactly_what_the_artefact_promises(name):
    spec = ARTEFACT["binaries"][name]
    result = capture.read_capture(BINARIES / name, spec["mime"])
    assert {
        "capture_instant": _instant(result.capture_instant),
        "capture_civil": result.capture_civil,
        "capture_source": result.capture_source,
    } == spec["expect"]


@pytest.mark.parametrize("name", sorted(ARTEFACT["binaries"]))
def test_binary_fixture_bytes_are_the_ones_the_artefact_names(name):
    # The artefact travels to iOS on its own; the sha256 is what ties it to these bytes, so an
    # edited fixture has to fail here rather than quietly re-specify the contract.
    spec = ARTEFACT["binaries"][name]
    raw = (BINARIES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == spec["sha256"]
    assert len(raw) == spec["size"]


@pytest.mark.parametrize("vector", ARTEFACT["vectors"]["exifStrings"], ids=lambda v: v["id"])
def test_exif_vectors(vector):
    result = capture.capture_from_exif(vector["datetime"], vector["offset"])
    assert {
        "instant": _instant(result.capture_instant),
        "civil": result.capture_civil,
        "source": result.capture_source,
    } == vector["expect"]


@pytest.mark.parametrize("vector", ARTEFACT["vectors"]["videoHeader"], ids=lambda v: v["id"])
def test_movie_header_vectors(vector):
    now = datetime.now(UTC)
    seconds = vector.get("seconds")
    if seconds is None:  # expressed relative to now (the future-clock rejection)
        seconds = int(
            (now + timedelta(days=vector["daysFromNow"]) - capture.QUICKTIME_EPOCH).total_seconds()
        )
    assert _instant(capture.instant_from_movie_header(seconds, now=now)) == vector["expect"]


@pytest.mark.parametrize("vector", ARTEFACT["vectors"]["videoCreationDate"], ids=lambda v: v["id"])
def test_quicktime_creationdate_vectors(vector):
    assert _instant(capture.parse_quicktime_creationdate(vector["text"])) == vector["expect"]


def test_the_video_reader_seeks_over_the_samples_instead_of_reading_them(tmp_path):
    """The whole reason a video costs no more than a photo here: `mdat` is never read.

    Without this the read is still CORRECT and still passes every vector above — it just pulls the
    entire file through memory, which is what made "route large videos through the worker" look
    necessary. Counting the bytes is the only way to tell the two implementations apart.
    """
    clip = tmp_path / "big.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=640x480:d=8:r=25",
            "-c:v", "libx264", "-crf", "5", "-pix_fmt", "yuv420p",
            "-metadata", "creation_time=2026-02-03T09:30:00Z", "-y", str(clip),
        ],
        check=True,
    )
    size = clip.stat().st_size
    assert size > 100_000, "the fixture has to be big enough for the difference to be visible"

    read_total = 0
    real_open = builtins.open

    class Counting:
        def __init__(self, fh):
            self._fh = fh

        def read(self, *args):
            nonlocal read_total
            data = self._fh.read(*args)
            read_total += len(data)
            return data

        def seek(self, *args):
            return self._fh.seek(*args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    try:
        capture.open = lambda path, mode: Counting(real_open(path, mode))  # type: ignore[attr-defined]
        result = capture.read_capture(clip, "video/mp4")
    finally:
        del capture.open  # back to the builtin
    assert _instant(result.capture_instant) == "2026-02-03T09:30:00Z"  # the clip's own header
    assert read_total < size // 10, f"read {read_total} of {size} bytes — the mdat was pulled in"


def test_an_unreadable_file_is_a_file_without_a_date(tmp_path):
    junk = tmp_path / "truncated.jpg"
    junk.write_bytes(b"\xff\xd8\xff\xe1garbage")
    assert capture.read_capture(junk, "image/jpeg") == capture.NOTHING
    assert capture.read_capture(tmp_path / "absent.mp4", "video/mp4") == capture.NOTHING
    assert capture.read_capture(BINARIES / "jpeg-offset.jpg", "application/pdf") == capture.NOTHING


# --------------------------------------------------------------------------------------------
# The inspection endpoint
# --------------------------------------------------------------------------------------------


def test_capture_is_pending_until_the_last_chunk_lands(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = (BINARIES / "jpeg-offset.jpg").read_bytes()
        uid = _start(client, "jpeg-offset.jpg", "image/jpeg", len(data))
        client.patch(f"/api/uploads/{uid}", content=data[:200], headers={"Upload-Offset": "0"})
        pending = client.get(f"/api/uploads/{uid}/capture").json()
        assert pending == {
            "state": "pending",
            "capture_instant": None,
            "capture_civil": None,
            "capture_source": None,
        }
        client.patch(f"/api/uploads/{uid}", content=data[200:], headers={"Upload-Offset": "200"})
        done = client.get(f"/api/uploads/{uid}/capture").json()
        assert done == {"state": "done", **_expect("jpeg-offset.jpg")}
    finally:
        _cleanup()


def test_capture_of_a_civil_only_photo_hands_the_wall_clock_over_unresolved(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _upload_fixture(client, "jpeg-no-offset.jpg")
        assert client.get(f"/api/uploads/{uid}/capture").json() == {
            "state": "done",
            **_expect("jpeg-no-offset.jpg"),
        }
    finally:
        _cleanup()


def test_capture_of_another_owners_upload_is_the_same_404_as_an_unknown_uid(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    other = _login_as(monkeypatch, "capture_other_owner")
    try:
        uid = _upload_fixture(client, "jpeg-offset.jpg")
        assert client.get(f"/api/uploads/{uid}/capture").status_code == 200
        foreign = other.get(f"/api/uploads/{uid}/capture")
        unknown = other.get("/api/uploads/does-not-exist/capture")
        assert foreign.status_code == 404
        assert foreign.json() == unknown.json()
        # and the victim's session is untouched by the refused read
        assert client.get(f"/api/uploads/{uid}/capture").json()["state"] == "done"
    finally:
        _cleanup()
        _drop_user("capture_other_owner")


def test_inspection_consumes_nothing(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _upload_fixture(client, "jpeg-offset.jpg")
        first = client.get(f"/api/uploads/{uid}/capture").json()
        again = client.get(f"/api/uploads/{uid}/capture").json()
        assert first == again  # idempotent
        assert client.head(f"/api/uploads/{uid}").status_code == 200  # session still open
        finalized = client.post(f"/api/uploads/{uid}/finalize")
        assert finalized.status_code == 200, finalized.text
        att = finalized.json()
        expected = _expect("jpeg-offset.jpg")
        assert att["capture_instant"] == expected["capture_instant"]
        assert att["capture_source"] == expected["capture_source"]
        assert att["captured_at"] == expected["capture_instant"]  # precedence: the file's instant
        assert att["position"] is None  # a standalone finalize links nothing
    finally:
        _cleanup()


def test_captured_at_falls_back_to_the_browser_mtime_when_the_file_says_nothing(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _upload_fixture(client, "png-none.png")
        att = client.post(f"/api/uploads/{uid}/finalize").json()
        assert {k: att[k] for k in _expect("png-none.png")} == _expect("png-none.png")
        assert att["captured_at"] == "2021-01-01T00:00:00Z"  # last_modified 1609459200000
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# The saved order
# --------------------------------------------------------------------------------------------


def test_create_saves_the_declared_order(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        a = _upload_bytes(client, "a.png", "image/png", _png(b"A"))
        b = _upload_bytes(client, "b.png", "image/png", _png(b"B"))
        c = _upload_bytes(client, "c.png", "image/png", _png(b"C"))
        memo = client.post(
            "/api/memos",
            json={"content": "three", "visibility": "PRIVATE", "upload_uids": [c, a, b]},
        ).json()
        got = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in got] == [0, 1, 2]
        # …and each place holds the FILE that was asked for there, not merely some attachment
        assert _filenames_in_order(memo["uid"]) == ["c.png", "a.png", "b.png"]
        assert [x["uid"] for x in got] == [x["uid"] for x in memo["attachments"]]
    finally:
        _cleanup()


def test_the_reverse_request_order_reads_back_reversed(logged_in_client, monkeypatch, tmp_path):
    """The order is the REQUEST's, not the upload's: the same two files, posted the other way
    round, come back the other way round."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        first, second = _png(b"1"), _png(b"2")
        a = _upload_bytes(client, "a.png", "image/png", first)
        b = _upload_bytes(client, "b.png", "image/png", second)
        forward = client.post(
            "/api/memos", json={"content": "f", "visibility": "PRIVATE", "upload_uids": [a, b]}
        ).json()
        names_forward = [x["uid"] for x in _memo_attachments(client, forward["uid"])]

        c = _upload_bytes(client, "c.png", "image/png", _png(b"3"))
        d = _upload_bytes(client, "d.png", "image/png", _png(b"4"))
        reverse = client.post(
            "/api/memos", json={"content": "r", "visibility": "PRIVATE", "upload_uids": [d, c]}
        ).json()
        atts = _memo_attachments(client, reverse["uid"])
        assert [x["position"] for x in atts] == [0, 1]
        eng = create_engine(SYNC_URL)
        with eng.connect() as conn:
            filenames = list(
                conn.execute(
                    text(
                        "SELECT a.filename FROM attachments a JOIN memos m ON m.id = a.memo_id "
                        "WHERE m.uid = :u ORDER BY a.position"
                    ),
                    {"u": reverse["uid"]},
                ).scalars()
            )
        eng.dispose()
        assert filenames == ["d.png", "c.png"]
        assert len(names_forward) == 2
    finally:
        _cleanup()


def test_a_legacy_null_row_sorts_last_and_is_renumbered_on_the_next_edit(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        a = _upload_bytes(client, "a.png", "image/png", _png(b"A"))
        b = _upload_bytes(client, "b.png", "image/png", _png(b"B"))
        memo = client.post(
            "/api/memos", json={"content": "legacy", "visibility": "PRIVATE", "upload_uids": [a, b]}
        ).json()
        _set_positions(memo["uid"], {0: None, 1: None})  # a memo from before the column
        before = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in before] == [None, None]  # ordered by id, as they always were

        new = _upload_bytes(client, "c.png", "image/png", _png(b"C"))
        patched = client.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [new]})
        assert patched.status_code == 200, patched.text
        after = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in after] == [0, 1, 2]
        # the two it already had keep the places they were displayed in; the new one lands last
        assert [x["uid"] for x in after[:2]] == [x["uid"] for x in before]
    finally:
        _cleanup()


def test_a_mixed_memo_is_renumbered_in_its_displayed_order_not_its_id_order(
    logged_in_client, monkeypatch, tmp_path
):
    """The case the round-5 re-check caught: numbering only the NULL rows would move them AHEAD of
    the numbered ones (NULLS LAST means a number always wins), silently reordering a memo somebody
    has already looked at."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        a = _upload_bytes(client, "a.png", "image/png", _png(b"A"))
        b = _upload_bytes(client, "b.png", "image/png", _png(b"B"))
        memo = client.post(
            "/api/memos", json={"content": "mixed", "visibility": "PRIVATE", "upload_uids": [a, b]}
        ).json()
        # A (lower id) NULL, B (higher id) at 0 → displayed [B, A]
        _set_positions(memo["uid"], {0: None, 1: 0})
        displayed = [x["uid"] for x in _memo_attachments(client, memo["uid"])]

        new = _upload_bytes(client, "c.png", "image/png", _png(b"C"))
        patched = client.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [new]})
        assert patched.status_code == 200, patched.text
        after = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in after] == [0, 1, 2]
        assert [x["uid"] for x in after[:2]] == displayed  # B then A — unchanged
    finally:
        _cleanup()


def test_an_edit_appends_after_a_fully_positioned_memo(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        a = _upload_bytes(client, "a.png", "image/png", _png(b"A"))
        b = _upload_bytes(client, "b.png", "image/png", _png(b"B"))
        memo = client.post(
            "/api/memos", json={"content": "set", "visibility": "PRIVATE", "upload_uids": [b, a]}
        ).json()
        before = [x["uid"] for x in _memo_attachments(client, memo["uid"])]
        c = _upload_bytes(client, "c.png", "image/png", _png(b"C"))
        d = _upload_bytes(client, "d.png", "image/png", _png(b"D"))
        assert (
            client.patch(f"/api/memos/{memo['uid']}", json={"upload_uids": [d, c]}).status_code
            == 200
        )
        after = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in after] == [0, 1, 2, 3]
        assert [x["uid"] for x in after[:2]] == before  # the saved order is a fact
        # and the appended pair keeps the order the edit asked for, after the existing two
        assert _filenames_in_order(memo["uid"])[2:] == ["d.png", "c.png"]
    finally:
        _cleanup()


def test_a_reused_attachment_takes_the_place_of_the_uid_that_named_it(
    logged_in_client, monkeypatch, tmp_path
):
    """Dedup hands back an attachment these bytes already made. Its place is where the CLIENT put
    the uid it sent, not wherever the reused row happens to sort."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        shared = _png(b"SHARED")
        first = _upload_bytes(client, "shared.png", "image/png", shared)
        reused_uid = client.post(f"/api/uploads/{first}/finalize").json()["uid"]

        a = _upload_bytes(client, "a.png", "image/png", _png(b"A"))
        again = _upload_bytes(client, "shared-again.png", "image/png", shared)
        memo = client.post(
            "/api/memos",
            json={"content": "dedup", "visibility": "PRIVATE", "upload_uids": [a, again]},
        ).json()
        atts = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in atts] == [0, 1]
        assert atts[1]["uid"] == reused_uid  # the reused row, in the place its uid was sent in
    finally:
        _cleanup()


def test_a_promoted_print_takes_the_place_of_its_own_uid(logged_in_client, monkeypatch, tmp_path):
    """A print with no screen in the batch is PROMOTED to a normal picture in pass 2 — and pass 2
    runs after pass 1, so its place has to come from the request, not from the pass order."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        printed = _upload_bytes(client, "_AR1 - print.png", "image/png", _png(b"P"))
        screen = _upload_bytes(client, "other.png", "image/png", _png(b"S"))
        memo = client.post(
            "/api/memos",
            json={"content": "print", "visibility": "PRIVATE", "upload_uids": [printed, screen]},
        ).json()
        atts = _memo_attachments(client, memo["uid"])
        assert [x["position"] for x in atts] == [0, 1]
        eng = create_engine(SYNC_URL)
        with eng.connect() as conn:
            names = list(
                conn.execute(
                    text(
                        "SELECT a.filename FROM attachments a JOIN memos m ON m.id = a.memo_id "
                        "WHERE m.uid = :u ORDER BY a.position"
                    ),
                    {"u": memo["uid"]},
                ).scalars()
            )
        eng.dispose()
        assert names == ["_AR1 - print.png", "other.png"]  # the declared order, both passes over
    finally:
        _cleanup()


def test_a_bound_print_master_takes_no_place_of_its_own(logged_in_client, monkeypatch, tmp_path):
    """A print that binds to its screen becomes a rendition, not an attachment — it rides with the
    screen and must not consume a position or leave a hole in the numbering."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        screen = _upload_bytes(client, "_AR2.png", "image/png", _png(b"SC"))
        master = _upload_bytes(client, "_AR2 - print.png", "image/png", _png(b"PR"))
        tail = _upload_bytes(client, "tail.png", "image/png", _png(b"TL"))
        memo = client.post(
            "/api/memos",
            json={
                "content": "screen+print",
                "visibility": "PRIVATE",
                "upload_uids": [screen, master, tail],
            },
        ).json()
        atts = _memo_attachments(client, memo["uid"])
        assert len(atts) == 2  # the print is a rendition of the screen
        # 0 and 1, not 0 and 2: `position` is the RANK in the declared order, and a bound print
        # occupies no rank at all. Leaving a hole where its uid sat would make the column mean
        # "index in a request nobody keeps" instead of "place in this memo".
        assert [x["position"] for x in atts] == [0, 1]
        assert atts[0]["hasPrint"] is True
    finally:
        _cleanup()


def test_the_memo_response_carries_the_capture_fields_through_the_upload_lane(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        photo = _upload_fixture(client, "jpeg-offset.jpg")
        assert client.get(f"/api/uploads/{photo}/capture").json()["state"] == "done"
        memo = client.post(
            "/api/memos",
            json={"content": "dated", "visibility": "PRIVATE", "upload_uids": [photo]},
        ).json()
        eng = create_engine(SYNC_URL)
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT a.capture_instant, a.capture_source, a.captured_at, a.position "
                    "FROM attachments a JOIN memos m ON m.id = a.memo_id WHERE m.uid = :u"
                ),
                {"u": memo["uid"]},
            ).one()
        eng.dispose()
        assert row.capture_instant == _expected_instant("jpeg-offset.jpg")
        assert row.capture_source == _expect("jpeg-offset.jpg")["capture_source"]
        assert row.captured_at == row.capture_instant  # the precedence, end to end
        assert row.position == 0
    finally:
        _cleanup()


# --------------------------------------------------------------------------------------------
# What the reader must never do
# --------------------------------------------------------------------------------------------


class _Forbidden(BaseException):
    """Raised by the guards below. NOT an Exception: `read_capture` swallows those by design, and
    a swallowed violation would be indistinguishable from a file that simply had no date."""


def test_no_fixture_is_ever_decoded(monkeypatch):
    """Not one of the seven reaches a pixel decoder.

    `Image.getexif()` used to be how the EXIF was read, and for a PNG with no eXIf chunk it falls
    through to `load()` — decoding every photo that has no date, which is the opposite of what
    this module promises. The guard has to cover `ImageFile.load` as well as `Image.load`, since
    that is the one the JPEG/PNG/HEIF plugins actually inherit.
    """
    from PIL import Image, ImageFile

    def decoded(*_args, **_kwargs):
        raise _Forbidden("the reader decoded pixels")

    monkeypatch.setattr(Image.Image, "load", decoded)
    monkeypatch.setattr(ImageFile.ImageFile, "load", decoded)
    for name, spec in ARTEFACT["binaries"].items():
        result = capture.read_capture(BINARIES / name, spec["mime"])
        assert {
            "capture_instant": _instant(result.capture_instant),
            "capture_civil": result.capture_civil,
            "capture_source": result.capture_source,
        } == spec["expect"]


def test_only_the_exif_sub_ifd_is_ever_asked_for(monkeypatch):
    """The whitelist, mechanically: one sub-IFD is requested and the GPS IFD is never walked.

    Also pins the log guard — PIL's TIFF plugin logs every tag it parses at DEBUG, so the level is
    held at INFO for the length of the read and restored afterwards.
    """
    from PIL import Image

    real_get_ifd = Image.Exif.get_ifd
    seen: list[int] = []
    levels: list[int] = []

    def guard(self, tag):
        seen.append(tag)
        if tag != capture.EXIF_IFD:
            raise _Forbidden(f"asked for IFD {tag:#x}")
        levels.append(logging.getLogger("PIL.TiffImagePlugin").getEffectiveLevel())
        return real_get_ifd(self, tag)

    before = logging.getLogger("PIL.TiffImagePlugin").level
    monkeypatch.setattr(Image.Exif, "get_ifd", guard)
    result = capture.read_capture(BINARIES / "jpeg-offset.jpg", "image/jpeg")

    assert _instant(result.capture_instant) == _expect("jpeg-offset.jpg")["capture_instant"]
    assert seen == [capture.EXIF_IFD]  # never 0x8825 (GPS), never anything else
    assert levels and all(level >= logging.INFO for level in levels)
    assert logging.getLogger("PIL.TiffImagePlugin").level == before  # and put back afterwards


# --------------------------------------------------------------------------------------------
# Order under concurrency, and the finalize/completion race
# --------------------------------------------------------------------------------------------


def test_a_promoted_print_takes_the_place_of_the_uid_that_asked_first(
    logged_in_client, monkeypatch, tmp_path
):
    """[orphan print (bytes A), screen B, screen (bytes A)] — the print is promoted in pass 2 and
    dedups onto the attachment pass 1 made from the identical bytes. The EARLIEST request index
    wins, so that attachment holds place 0; keeping the first FINALIZED index instead would read
    back as [B, A] — an order nobody asked for."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        same = _png(b"SAME")
        orphan_print = _upload_bytes(client, "_ZZ9 - print.png", "image/png", same)
        screen_b = _upload_bytes(client, "b.png", "image/png", _png(b"B"))
        screen_a = _upload_bytes(client, "_AR9.png", "image/png", same)
        memo = client.post(
            "/api/memos",
            json={
                "content": "dedup across the passes",
                "visibility": "PRIVATE",
                "upload_uids": [orphan_print, screen_b, screen_a],
            },
        ).json()
        atts = _memo_attachments(client, memo["uid"])
        assert len(atts) == 2  # the print's bytes are the screen's bytes: one attachment
        assert [x["position"] for x in atts] == [0, 1]
        assert _filenames_in_order(memo["uid"]) == ["_AR9.png", "b.png"]
    finally:
        _cleanup()
