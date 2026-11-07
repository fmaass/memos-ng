"""The trip door refuses every file name and field home's `validate_batch` would quarantine
(MEMNG-1200).

Home quarantines a WHOLE batch for one entry it will not file, so a value the door accepted and
home refuses costs the guest every picture they sent with it (live: a file named `.x.jpg`). Each
case here is first shown to be a home refusal — `trip_ingest.validate_blob` / `validate_batch` on
the same value — and then to be a 4xx at the door endpoint that sees the field, with nothing
filed. The controls are values home files: the door must keep accepting those.
"""

import asyncio
import json

import pytest
from app.ingest import core
from app.ingest.factory import create_app
from app.services import trip_ingest
from httpx import ASGITransport, AsyncClient

from tests.ingest_support import (
    JPEG,
    PNG,
    bearer,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    redeem,
    seeded_pass,
    upload,
)
from tests.test_attachments_taken import _sql

SHA = "0" * 64


def _home_blob(name: str) -> dict:
    return {"sha256": SHA, "size": 5, "mime": "image/jpeg", "filename": name}


def _home_batch(caption: object) -> dict:
    return {
        "batch_id": "b",
        "pass_uid": "p",
        "device_id": "d",
        "caption": caption,
        "blobs": [_home_blob("a.jpg")],
    }


@pytest.fixture
async def answering(ingest_env):  # noqa: F811
    """The door as a guest meets it: a crash answers as the 500 a guest would get, never as an
    exception in the test, so a refusal that is not a 4xx shows as its status."""
    async with AsyncClient(
        transport=ASGITransport(app=create_app("ingest"), raise_app_exceptions=False),
        base_url="http://door",
    ) as client:
        yield client


async def _token(client: AsyncClient, home: AsyncClient) -> str:
    _, secret = await seeded_pass(home)
    return (await redeem(client, secret))["device_token"]


def _raw_json(body: dict) -> dict:
    # json.dumps escapes NUL and a lone surrogate as \u escapes, as a hostile client may send.
    return {"content": json.dumps(body), "headers": {"content-type": "application/json"}}


NAME_REFUSED = {"detail": "This file name is not accepted"}


@pytest.mark.parametrize(
    ("name", "status"),
    [
        pytest.param(".x.jpg", 415, id="leading-dot"),
        pytest.param("dir/a.jpg", 415, id="slash"),
        pytest.param("dir\\a.jpg", 415, id="backslash"),
        pytest.param("a\x00.jpg", 415, id="nul"),
        pytest.param("a\n.jpg", 415, id="control-char"),
        pytest.param("a\u200b.jpg", 415, id="format-char"),
        # Not a string pydantic takes: refused as request validation, before the handler runs.
        pytest.param("a\ud800.jpg", 422, id="lone-surrogate"),
    ],
)
async def test_a_name_home_would_quarantine_is_refused_at_create(
    answering,
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    name,
    status,
):
    token = await _token(answering, home_iface)
    body = {"filename": name, "mime": "image/jpeg", "size": 5}
    raw = _raw_json(body)
    r = await answering.post(
        "/api/guest/uploads", content=raw["content"], headers={**raw["headers"], **bearer(token)}
    )
    # 415 as for a name outside the type allowlist: the guest's client shows the file as one it
    # cannot send, rather than offering a retry the door would refuse again.
    assert r.status_code == status, r.text
    if status == 415:
        assert r.json() == NAME_REFUSED
    assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]
    assert sorted(ingest_env.rglob("*.part")) == []
    # The case is a home refusal: the whole batch carrying it would be quarantined.
    with pytest.raises(trip_ingest.Hostile):
        trip_ingest.validate_blob(_home_blob(name))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("IMG 0001.jpg", id="space"),
        pytest.param(" .x.jpg", id="dot-after-a-space"),
        pytest.param("a.x.jpg", id="inner-dot"),
        pytest.param("Ünïcødé 写真.jpg", id="unicode"),
        pytest.param("n" * 508 + ".jpg", id="512-chars"),
    ],
)
async def test_a_name_home_files_is_still_accepted_at_create(
    answering,
    home_iface,  # noqa: F811
    name,
):
    token = await _token(answering, home_iface)
    raw = _raw_json({"filename": name, "mime": "image/jpeg", "size": 5})
    r = await answering.post(
        "/api/guest/uploads", content=raw["content"], headers={**raw["headers"], **bearer(token)}
    )
    assert r.status_code == 201, r.text
    assert trip_ingest.validate_blob(_home_blob(name)).filename == name


async def test_two_concurrent_creates_with_a_refused_name_both_answer_415(
    answering,
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
):
    token = await _token(answering, home_iface)
    raw = _raw_json({"filename": ".x.jpg", "mime": "image/jpeg", "size": 5})
    rs = await asyncio.gather(
        *(
            answering.post(
                "/api/guest/uploads",
                content=raw["content"],
                headers={**raw["headers"], **bearer(token)},
            )
            for _ in range(2)
        )
    )
    assert [r.status_code for r in rs] == [415, 415]
    assert _sql("SELECT count(*) FROM guest_uploads") == [(0,)]


@pytest.mark.parametrize(
    "caption",
    [
        pytest.param("hello\x00", id="nul"),
        pytest.param("hello \ud800", id="lone-surrogate"),
    ],
)
async def test_a_caption_home_would_quarantine_is_refused_at_completion(
    answering,
    home_iface,  # noqa: F811
    caption,
):
    token = await _token(answering, home_iface)
    uids = [await upload(answering, token, JPEG)]
    raw = _raw_json({"upload_uids": uids, "caption": caption})
    r = await answering.post(
        "/api/guest/memos", content=raw["content"], headers={**raw["headers"], **bearer(token)}
    )
    assert r.status_code == 422, r.text
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]
    # Nothing was spent: the same uploads complete under a caption home files.
    r = await answering.post(
        "/api/guest/memos", json={"upload_uids": uids, "caption": "hello"}, headers=bearer(token)
    )
    assert r.status_code == 200, r.text
    with pytest.raises(trip_ingest.Hostile):
        trip_ingest.validate_batch(_home_batch(caption))


async def test_a_batch_over_the_batch_byte_limit_is_refused_at_completion(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    """Home refuses a batch past 50 GB; the door's pass byte cap is pushed by home and is not
    bound to that limit, so the completion checks the one limit itself. Lowered to two small
    files' worth so the rule is reachable in a test; `raising=False` lets the run on a tree
    without the shared limit answer with what its door does. `door`, not `answering`: the guest
    schema test runs this one as the provoker of the completion's 413."""
    token = await _token(door, home_iface)
    uids = [await upload(door, token, JPEG), await upload(door, token, PNG, "b.png")]
    total = len(JPEG) + len(PNG)
    monkeypatch.setattr(core, "MAX_BATCH_BYTES", total - 1, raising=False)
    r = await door.post("/api/guest/memos", json={"upload_uids": uids}, headers=bearer(token))
    assert r.status_code == 413, r.text
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]
    assert _sql("SELECT count(*) FROM guest_uploads") == [(2,)]
    home = {
        **_home_batch(None),
        "blobs": [
            {**_home_blob("a.jpg"), "size": len(JPEG)},
            {**_home_blob("b.png"), "sha256": "1" * 64, "mime": "image/png", "size": len(PNG)},
        ],
    }
    with pytest.raises(trip_ingest.Hostile):
        trip_ingest.validate_batch(home)
    # At the limit exactly, the batch files, at the door and at home.
    monkeypatch.setattr(core, "MAX_BATCH_BYTES", total)
    r = await door.post("/api/guest/memos", json={"upload_uids": uids}, headers=bearer(token))
    assert r.status_code == 200 and r.json()["byte_total"] == total, r.text
    assert len(trip_ingest.validate_batch(home).blobs) == 2


async def test_two_concurrent_completions_over_the_byte_limit_both_answer_413(
    answering,
    home_iface,  # noqa: F811
    monkeypatch,
):
    token = await _token(answering, home_iface)
    uids = [await upload(answering, token, JPEG), await upload(answering, token, PNG, "b.png")]
    monkeypatch.setattr(core, "MAX_BATCH_BYTES", len(JPEG) + len(PNG) - 1, raising=False)
    rs = await asyncio.gather(
        *(
            answering.post("/api/guest/memos", json={"upload_uids": uids}, headers=bearer(token))
            for _ in range(2)
        )
    )
    assert [r.status_code for r in rs] == [413, 413]
    assert _sql("SELECT count(*) FROM ingest_batches") == [(0,)]


def test_the_device_name_rule_home_shares_text_ok_with_still_holds():
    """Moving the text rule into the shared module reaches home's device mirror too: a display
    name PostgreSQL cannot store is still refused there, and a plain one still kept."""
    good = {"device_id": "d", "pass_uid": "p", "display_name": "Anna", "platform": "web"}
    good |= {"uploads_count": 0, "bytes_total": 0, "first_seen": "2026-11-07T00:00:00Z"}
    assert trip_ingest._valid_device(good) is not None
    for bad in ("An\x00na", "An\ud800na"):
        assert trip_ingest._valid_device({**good, "display_name": bad}) is None
