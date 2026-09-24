"""The tus 1.0 surface (`/api/tus`: core + creation + expiration) over the existing upload store.

Every expected value here is a literal the protocol or the client fixes — a status code, a header
value TUSKit sends, a millisecond it must get back — or a byte count read back from the database
and the part file after the fact, never a value derived from the code under test.
"""

import asyncio
import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest
from app.config import settings
from app.database import async_session
from app.main import app
from app.models import Upload
from app.services import uploads as svc
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL
from tests.test_uploads import _chunks, _cleanup, _drop_user, _login_as, _owner

TUS = {"Tus-Resumable": "1.0.0"}
OFFSET_STREAM = "application/offset+octet-stream"
IMF_FIXDATE = re.compile(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$")


def _b64(value: bytes | str) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return base64.b64encode(raw).decode()


def _metadata(**pairs: bytes | str) -> str:
    """An `Upload-Metadata` header written the way TUSKit composes it — `key <base64>` pairs joined
    by commas — built by hand so the router is not tested through its own codec."""
    return ",".join(f"{key} {_b64(value)}" for key, value in pairs.items())


def _decoded_metadata(header: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for pair in header.split(","):
        key, _, value = pair.strip().partition(" ")
        out[key] = base64.b64decode(value)
    return out


def _create_headers(size: int, metadata: str | None = None) -> dict[str, str]:
    headers = {**TUS, "Upload-Length": str(size)}
    headers["Upload-Metadata"] = (
        _metadata(filename="clip.mov", filetype="video/quicktime") if metadata is None else metadata
    )
    return headers


def _patch_headers(offset: int) -> dict[str, str]:
    return {**TUS, "Upload-Offset": str(offset), "Content-Type": OFFSET_STREAM}


def _uid_from(location: str) -> str:
    assert location.startswith("/api/tus/"), location  # path-absolute: TUSKit resolves it
    return location.removeprefix("/api/tus/")


def _row(uid: str):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text(
                "SELECT bytes_received, declared_size, expires_at, client_mtime, mime, filename "
                "FROM uploads WHERE upload_uid = :u"
            ),
            {"u": uid},
        ).one_or_none()
    eng.dispose()
    return row


def _upload_count() -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        n = c.execute(text("SELECT count(*) FROM uploads")).scalar_one()
    eng.dispose()
    return n


def _assert_expires_matches_row(header: str, uid: str) -> None:
    assert IMF_FIXDATE.match(header), header
    expires_at = _row(uid).expires_at
    assert parsedate_to_datetime(header) == expires_at.replace(microsecond=0)


async def _tus_patch_cut_short(token, uid, offset, pieces, content_length):
    """A tus PATCH through the COMPLETE app whose client connection drops mid-body: each piece
    arrives as a `more_body` request message, then every later receive is `http.disconnect` — the
    messages uvicorn sends when the socket goes (the pattern of tests/test_uploads.py
    `_patch_cut_short`)."""
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {token}".encode()),
        (b"tus-resumable", b"1.0.0"),
        (b"upload-offset", str(offset).encode()),
        (b"content-type", OFFSET_STREAM.encode()),
        (b"content-length", str(content_length).encode()),
    ]
    path = f"/api/tus/{uid}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PATCH",
        "headers": headers,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }
    queue = [{"type": "http.request", "body": p, "more_body": True} for p in pieces]
    sent = []

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


# ---------------------------------------------------------------------------------------------
# OPTIONS — the capability probe, unauthenticated by decision
# ---------------------------------------------------------------------------------------------


def test_options_answers_the_capabilities_without_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 7)
    r = client.options("/api/tus")  # no credential, no Tus-Resumable: OPTIONS is exempt from both
    assert r.status_code == 204
    assert r.headers["Tus-Resumable"] == "1.0.0"
    assert r.headers["Tus-Version"] == "1.0.0"
    assert set(r.headers["Tus-Extension"].split(",")) == {"creation", "expiration"}
    assert r.headers["Tus-Max-Size"] == str(7 * 1048576)
    assert r.content == b""


def test_the_data_verbs_require_a_credential(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    before = _upload_count()
    assert client.post("/api/tus", headers=_create_headers(10)).status_code == 401
    assert client.head("/api/tus/whatever", headers=TUS).status_code == 401
    assert (
        client.patch("/api/tus/whatever", content=b"x", headers=_patch_headers(0)).status_code
        == 401
    )
    assert _upload_count() == before


# ---------------------------------------------------------------------------------------------
# The contract round trip: create → HEAD → PATCH → cut → HEAD → resume → HEAD → finalize
# ---------------------------------------------------------------------------------------------


async def test_create_head_patch_cut_resume_finalize_round_trip(pat_client, monkeypatch, tmp_path):
    bearer, _, token = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    content = bytes(range(256)) * 400  # 102400 bytes
    try:
        created = await bearer.post("/api/tus", headers=_create_headers(len(content)))
        assert created.status_code == 201
        assert created.content == b""
        assert created.headers["Tus-Resumable"] == "1.0.0"
        uid = _uid_from(created.headers["Location"])
        _assert_expires_matches_row(created.headers["Upload-Expires"], uid)

        head = await bearer.head(f"/api/tus/{uid}", headers=TUS)
        assert head.status_code == 200
        assert head.headers["Upload-Offset"] == "0"
        assert head.headers["Upload-Length"] == str(len(content))
        assert head.headers["Cache-Control"] == "no-store"
        assert head.headers["Tus-Resumable"] == "1.0.0"
        _assert_expires_matches_row(head.headers["Upload-Expires"], uid)

        first = await bearer.patch(
            f"/api/tus/{uid}", content=content[:30000], headers=_patch_headers(0)
        )
        assert first.status_code == 204
        assert first.content == b""
        assert first.headers["Upload-Offset"] == "30000"
        assert first.headers["Tus-Resumable"] == "1.0.0"
        _assert_expires_matches_row(first.headers["Upload-Expires"], uid)

        # The connection drops after 15000 more bytes of a body that announced the whole rest.
        status = await _tus_patch_cut_short(
            token, uid, 30000, [content[30000:40000], content[40000:45000]], len(content) - 30000
        )
        assert status == 204
        head = await bearer.head(f"/api/tus/{uid}", headers=TUS)
        assert head.headers["Upload-Offset"] == "45000"  # the bytes that arrived are kept
        assert list((tmp_path / "uploads").glob("*.spool")) == []

        rest = await bearer.patch(
            f"/api/tus/{uid}", content=content[45000:], headers=_patch_headers(45000)
        )
        assert rest.status_code == 204
        assert rest.headers["Upload-Offset"] == str(len(content))

        head = await bearer.head(f"/api/tus/{uid}", headers=TUS)
        assert head.headers["Upload-Offset"] == head.headers["Upload-Length"] == str(len(content))

        fin = await bearer.post(f"/api/uploads/{uid}/finalize")
        assert fin.status_code == 200
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            sha = c.execute(
                text("SELECT sha256 FROM attachments WHERE uid=:u"), {"u": fin.json()["uid"]}
            ).scalar_one()
        eng.dispose()
        assert sha == hashlib.sha256(content).hexdigest()
    finally:
        _cleanup()


# ---------------------------------------------------------------------------------------------
# Refusals: 409, 412, 415, 413, 400, 404
# ---------------------------------------------------------------------------------------------


def _create(client, size: int, metadata: str | None = None) -> str:
    r = client.post("/api/tus", headers=_create_headers(size, metadata))
    assert r.status_code == 201, r.text
    return _uid_from(r.headers["Location"])


def test_a_patch_at_a_stale_offset_is_409_and_modifies_nothing(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(client, 1000)
        r = client.patch(f"/api/tus/{uid}", content=b"A" * 500, headers=_patch_headers(0))
        assert r.status_code == 204
        part_before = svc.part_path(uid).read_bytes()

        for stale in (0, 499, 501):  # a replay, a partial replay, a forward gap
            r = client.patch(f"/api/tus/{uid}", content=b"Z" * 100, headers=_patch_headers(stale))
            assert r.status_code == 409, (stale, r.status_code)
            assert r.headers["Tus-Resumable"] == "1.0.0"

        assert _row(uid).bytes_received == 500
        assert svc.part_path(uid).read_bytes() == part_before  # the BYTES, not just the size
        assert list((tmp_path / "uploads").glob("*.spool")) == []
    finally:
        _cleanup()


def test_every_verb_but_options_needs_tus_resumable_1_0_0(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(client, 1000)
        before = _upload_count()
        for version in (None, "0.2.2"):
            tus = {} if version is None else {"Tus-Resumable": version}

            create = _create_headers(10)
            del create["Tus-Resumable"]
            r = client.post("/api/tus", headers={**create, **tus})
            assert r.status_code == 412 and r.headers["Tus-Version"] == "1.0.0"

            r = client.head(f"/api/tus/{uid}", headers=tus)
            assert r.status_code == 412 and r.headers["Tus-Version"] == "1.0.0"
            assert "Upload-Offset" not in r.headers

            patch = _patch_headers(0)
            del patch["Tus-Resumable"]
            r = client.patch(f"/api/tus/{uid}", content=b"x" * 10, headers={**patch, **tus})
            assert r.status_code == 412 and r.headers["Tus-Version"] == "1.0.0"

        assert _upload_count() == before  # no refused POST created a session
        assert _row(uid).bytes_received == 0  # no refused PATCH landed
    finally:
        _cleanup()


def test_a_patch_that_is_not_an_offset_stream_is_415(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(client, 1000)
        for content_type in ("application/octet-stream", None):
            headers = _patch_headers(0)
            if content_type is None:
                del headers["Content-Type"]
            else:
                headers["Content-Type"] = content_type
            r = client.patch(f"/api/tus/{uid}", content=b"x" * 10, headers=headers)
            assert r.status_code == 415, content_type
        assert _row(uid).bytes_received == 0
    finally:
        _cleanup()


def test_creation_over_the_ceiling_is_413_and_at_it_is_accepted(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)
    try:
        before = _upload_count()
        r = client.post("/api/tus", headers=_create_headers(1048577))
        assert r.status_code == 413
        assert _upload_count() == before
        assert client.post("/api/tus", headers=_create_headers(1048576)).status_code == 201
    finally:
        _cleanup()


def test_a_patch_past_the_declared_length_is_413(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(client, 100)
        r = client.patch(f"/api/tus/{uid}", content=b"x" * 101, headers=_patch_headers(0))
        assert r.status_code == 413
        assert _row(uid).bytes_received == 0
    finally:
        _cleanup()


@pytest.mark.parametrize("length", [None, "", "abc", "-5", "1.5", "+5"])
def test_creation_without_a_usable_upload_length_is_400(
    logged_in_client, monkeypatch, tmp_path, length
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    headers = _create_headers(10)
    if length is None:
        del headers["Upload-Length"]
    else:
        headers["Upload-Length"] = length
    before = _upload_count()
    r = client.post("/api/tus", headers=headers)
    assert r.status_code == 400
    assert "Upload-Length" in r.json()["detail"]
    assert _upload_count() == before


@pytest.mark.parametrize("offset", [None, "", "abc", "-1", "+0"])
def test_a_patch_without_a_usable_upload_offset_is_400(
    logged_in_client, monkeypatch, tmp_path, offset
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(client, 100)
        headers = _patch_headers(0)
        if offset is None:
            del headers["Upload-Offset"]
        else:
            headers["Upload-Offset"] = offset
        r = client.patch(f"/api/tus/{uid}", content=b"x" * 10, headers=headers)
        assert r.status_code == 400
        assert "Upload-Offset" in r.json()["detail"]
        assert _row(uid).bytes_received == 0
    finally:
        _cleanup()


def test_another_users_uid_answers_exactly_as_an_unknown_one(
    logged_in_client, monkeypatch, tmp_path
):
    victim, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(victim, 1000)
        assert (
            victim.patch(f"/api/tus/{uid}", content=b"V" * 400, headers=_patch_headers(0))
        ).status_code == 204
        attacker = _login_as(monkeypatch, "tus_thief")
        ghost = "a-uid-that-never-existed"

        head, head_ghost = (attacker.head(f"/api/tus/{x}", headers=TUS) for x in (uid, ghost))
        assert head.status_code == 404
        assert (head.status_code, head.text) == (head_ghost.status_code, head_ghost.text)
        assert "Upload-Offset" not in head.headers and "Upload-Length" not in head.headers

        patch, patch_ghost = (
            attacker.patch(f"/api/tus/{x}", content=b"X" * 100, headers=_patch_headers(400))
            for x in (uid, ghost)
        )
        assert patch.status_code == 404
        assert (patch.status_code, patch.text) == (patch_ghost.status_code, patch_ghost.text)

        assert _row(uid).bytes_received == 400
        assert svc.part_path(uid).read_bytes() == b"V" * 400
    finally:
        _cleanup()
        _drop_user("tus_thief")


# ---------------------------------------------------------------------------------------------
# Upload-Metadata: restricted acceptance, faithful echo
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sent", ["1001", "1700000000123", "253402300799999"])
def test_last_modified_echoes_exactly_as_sent(logged_in_client, monkeypatch, tmp_path, sent):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        metadata = _metadata(filename="IMG_0001.HEIC", filetype="image/heic", lastModified=sent)
        uid = _create(client, 10, metadata)
        head = client.head(f"/api/tus/{uid}", headers=TUS)
        assert _decoded_metadata(head.headers["Upload-Metadata"]) == {
            "filename": b"IMG_0001.HEIC",
            "filetype": b"image/heic",
            "lastModified": sent.encode(),
        }
    finally:
        _cleanup()


def test_metadata_without_last_modified_echoes_only_what_was_sent(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uid = _create(client, 10, _metadata(filename="Grüße.txt", filetype="text/plain"))
        head = client.head(f"/api/tus/{uid}", headers=TUS)
        assert _decoded_metadata(head.headers["Upload-Metadata"]) == {
            "filename": "Grüße.txt".encode(),
            "filetype": b"text/plain",
        }
        row = _row(uid)
        assert (row.filename, row.mime, row.client_mtime) == ("Grüße.txt", "text/plain", None)
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("metadata", "named"),
    [
        (_metadata(filename="a", filetype="b", lastModified="0"), "lastModified"),
        (_metadata(filename="a", filetype="b", lastModified="0001"), "lastModified"),
        (_metadata(filename="a", filetype="b", lastModified="+5"), "lastModified"),
        (_metadata(filename="a", filetype="b", lastModified="253402300800000"), "lastModified"),
        (_metadata(filename="a", filetype="b", lastModified="12a"), "lastModified"),
        (_metadata(filename="a", filetype="b", lastModified="1 "), "lastModified"),
        (_metadata(filename="a", filetype="b", color="blue"), "color"),
        (_metadata(filename="a"), "filetype"),
        (_metadata(filetype="b"), "filename"),
        (_metadata(filename=b"\xff\xfe", filetype="b"), "filename"),
        (_metadata(filename="a", filetype=b"\xc3"), "filetype"),
        ("filename,filetype " + _b64("b"), "filename"),  # a required key sent without a value
        ("filename !!!!,filetype " + _b64("b"), "malformed"),  # not base64
        (_metadata(filename="a", filetype="b") + ",filename " + _b64("c"), "malformed"),
        (_metadata(filename="a", filetype="b") + ",", "malformed"),
        ("", "malformed"),
        # past Python's int() digit limit: must be refused by the grammar, never reach int()
        (_metadata(filename="a", filetype="b", lastModified="9" * 5000), "lastModified"),
    ],
)
def test_creation_refuses_metadata_it_could_not_echo_faithfully(
    logged_in_client, monkeypatch, tmp_path, metadata, named
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    before = _upload_count()
    r = client.post("/api/tus", headers=_create_headers(10, metadata))
    assert r.status_code == 400, (metadata, r.status_code)
    assert named in r.json()["detail"]
    assert _upload_count() == before


def test_creation_without_upload_metadata_is_400(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    headers = _create_headers(10)
    del headers["Upload-Metadata"]
    before = _upload_count()
    r = client.post("/api/tus", headers=headers)
    assert r.status_code == 400
    assert "filename" in r.json()["detail"]
    assert _upload_count() == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("filename", "a" * 513),
        ("filetype", "t" * 256),
        ("filename", "clip\x00.mov"),  # Postgres text cannot hold a NUL
        ("filetype", "video/\x00quicktime"),
    ],
)
def test_metadata_the_row_cannot_hold_is_refused_before_any_file_exists(
    logged_in_client, monkeypatch, tmp_path, key, value
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    pairs = {"filename": "clip.mov", "filetype": "video/quicktime", key: value}
    before = _upload_count()
    try:
        r = client.post("/api/tus", headers=_create_headers(10, _metadata(**pairs)))
        assert r.status_code == 400, r.status_code
        assert key in r.json()["detail"]
        assert _upload_count() == before
        assert list((tmp_path / "uploads").glob("*")) == []  # no orphaned part file
    finally:
        _cleanup()


def test_metadata_at_the_column_bounds_is_accepted_and_echoed(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        filename, filetype = "é" * 512, "t" * 255  # characters, not bytes: 1024 bytes of name
        uid = _create(client, 10, _metadata(filename=filename, filetype=filetype))
        head = client.head(f"/api/tus/{uid}", headers=TUS)
        assert _decoded_metadata(head.headers["Upload-Metadata"]) == {
            "filename": filename.encode(),
            "filetype": filetype.encode(),
        }
    finally:
        _cleanup()


def test_a_header_shaped_filename_travels_only_base64_encoded(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    evil = "a.txt\r\nLocation: https://evil.example/\r\nX-Injected: 1"
    try:
        created = client.post(
            "/api/tus", headers=_create_headers(10, _metadata(filename=evil, filetype="text/plain"))
        )
        assert created.status_code == 201
        uid = _uid_from(created.headers["Location"])
        assert created.headers.get_list("Location") == [f"/api/tus/{uid}"]
        assert "X-Injected" not in created.headers

        head = client.head(f"/api/tus/{uid}", headers=TUS)
        assert "X-Injected" not in head.headers and "Location" not in head.headers
        raw = head.headers["Upload-Metadata"]
        assert "\r" not in raw and "\n" not in raw and "evil.example" not in raw
        assert _decoded_metadata(raw)["filename"] == evil.encode()
        assert _row(uid).filename == evil
    finally:
        _cleanup()


def test_metadata_codec_holds_the_spec_example():
    from app.services.tus import encode_upload_metadata, parse_upload_metadata

    spec = "filename d29ybGQucG5n,is_confidential"
    parsed = parse_upload_metadata(spec)
    assert parsed == {"filename": b"world.png", "is_confidential": b""}
    assert encode_upload_metadata(parsed) == spec
    binary = {"blob": b"\x00\xff\x10", "name": "ü".encode()}
    assert parse_upload_metadata(encode_upload_metadata(binary)) == binary
    assert parse_upload_metadata("filename d29ybGQucG5n, is_confidential") == parsed
    assert parse_upload_metadata("blob " + _b64(b"a\x00b")) == {"blob": b"a\x00b"}


def test_upload_expires_is_an_imf_fixdate_in_gmt_or_absent():
    from app.services.tus import expires_header

    zurich_summer = timezone(timedelta(hours=2))
    assert expires_header(datetime(2026, 9, 25, 10, 30, 15, 999999, tzinfo=zurich_summer)) == {
        "Upload-Expires": "Fri, 25 Sep 2026 08:30:15 GMT"
    }
    assert expires_header(None) == {}  # a row that never expires promises no deadline


@pytest.mark.parametrize(
    "header",
    [
        "",
        ",",
        "filename d29ybGQucG5n,",
        ",filename d29ybGQucG5n",
        "filename d29ybGQucG5n extra",
        "filename d29ybGQucG5",  # bad padding
        "filename !!!!",
        "filename d29ybGQucG5n,filename d29ybGQucG5n",
    ],
)
def test_metadata_codec_refuses_a_malformed_header(header):
    from app.services.tus import parse_upload_metadata

    with pytest.raises(ValueError):
        parse_upload_metadata(header)


# ---------------------------------------------------------------------------------------------
# The store: strict offsets at both decision points; the sent millisecond kept exactly
# ---------------------------------------------------------------------------------------------


async def test_strict_offset_refuses_on_the_snapshot_without_consuming_the_body(
    adb, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "tus_strict_snapshot")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="s.bin", mime="application/octet-stream", size=900
    )
    await svc.append_stream(adb, up, 0, _chunks([b"A" * 300]))
    pulled = False

    async def body():
        nonlocal pulled
        pulled = True
        yield b"Z" * 100

    for stale in (0, 299, 301):
        with pytest.raises(ValueError):
            await svc.append_stream(adb, up, stale, body(), strict_offset=True)
    assert not pulled  # decided on the snapshot, before the body was read
    assert up.bytes_received == 300
    assert svc.part_path(up.upload_uid).read_bytes() == b"A" * 300

    # the default keeps today's tus-style contract: a replay is a no-op, not a refusal
    replay = await svc.append_stream(adb, up, 0, body())
    assert replay is not None and replay.upload.bytes_received == 300


async def test_strict_offset_refuses_under_the_row_lock_when_the_snapshot_went_stale(
    db_engine, monkeypatch, tmp_path
):
    # B's snapshot is true when B starts (offset 0), and B's body stays inside the ceiling, so the
    # only thing that can refuse it is the decision re-taken under the row lock after A committed.
    # Two real sessions, not the `adb` fixture's one transaction: A's append is COMMITTED before B
    # asks for the lock, as it would be between two requests.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner_id = c.execute(
            text("INSERT INTO users (subject, role) VALUES ('tus_strict_lock','user') RETURNING id")
        ).scalar_one()
    eng.dispose()
    try:
        async with async_session() as s0:
            up = await svc.create_upload(
                s0, owner_id=owner_id, filename="l.bin", mime="application/octet-stream", size=900
            )
            await s0.commit()
            uid = up.upload_uid

        async def fetch(session):
            return (
                await session.execute(select(Upload).where(Upload.upload_uid == uid))
            ).scalar_one()

        b_in_flight, a_committed = asyncio.Event(), asyncio.Event()

        async def b_body():
            yield b"B" * 100
            b_in_flight.set()
            await a_committed.wait()
            yield b"B" * 100

        async with async_session() as sa, async_session() as sb:
            b_snapshot = await fetch(sb)
            b_task = asyncio.create_task(
                svc.append_stream(sb, b_snapshot, 0, b_body(), strict_offset=True)
            )
            await b_in_flight.wait()
            await svc.append_stream(sa, await fetch(sa), 0, _chunks([b"A" * 500]))
            await sa.commit()
            a_committed.set()
            with pytest.raises(ValueError):
                await b_task
            await sb.rollback()

        assert _row(uid).bytes_received == 500
        assert svc.part_path(uid).read_bytes() == b"A" * 500
        assert list((tmp_path / "uploads").glob("*.spool")) == []
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM uploads WHERE owner_id = :o"), {"o": owner_id})
            c.execute(text("DELETE FROM users WHERE id = :o"), {"o": owner_id})
        eng.dispose()


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        (1001, datetime(1970, 1, 1, 0, 0, 1, 1000, tzinfo=UTC)),
        (1700000000123, datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)),
        (253402300799999, datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)),
        (None, None),
        (0, None),
    ],
)
async def test_create_upload_stores_the_sent_millisecond_exactly(
    adb, monkeypatch, tmp_path, sent, stored
):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "tus_mtime")
    up = await svc.create_upload(
        adb, owner_id=o.id, filename="m.bin", mime="x/y", size=1, last_modified=sent
    )
    reread = (
        await adb.execute(
            select(Upload)
            .where(Upload.upload_uid == up.upload_uid)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reread.client_mtime == stored
