"""POST /api/uploads/batch — many upload sessions in one request.

A share extension holding thirty files cannot afford thirty session POSTs before it is killed, so
one request opens them all. Each entry is the single create's body and answers what the single
create would have answered for it: an `UploadOut` (a staging dedupe hit born complete included),
or a refusal of that entry alone — 413 over the size limit, 507 when the volume cannot hold it on
top of the bytes the batch's earlier entries already claimed. The admitted sessions are created in
ONE transaction: a failure part-way leaves no row and no part file behind.

The route under test is real; only the free-space probe (`storage.free_bytes`) and, for the
forced failure, the service create are stubbed.
"""

import asyncio
import os
from pathlib import Path

import pytest
from app.config import settings
from app.main import app
from app.services import storage
from app.services import uploads as svc
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL
from tests.test_upload_staging_dedupe import ONE, _att, _ready_memo, _sha
from tests.test_upload_staging_dedupe import _cleanup as _cleanup_all

BATCH = "/api/uploads/batch"


def _rows() -> list[tuple]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        out = [
            tuple(r)
            for r in c.execute(
                text(
                    "SELECT upload_uid, filename, declared_size, bytes_received FROM uploads "
                    "ORDER BY filename"
                )
            )
        ]
    eng.dispose()
    return out


def _parts(data_root: Path) -> list[Path]:
    return sorted((data_root / "uploads").glob("*.part"))


def _entry(name: str, size: int = 10, **extra) -> dict:
    return {"filename": name, "mime": "image/jpeg", "size": size, **extra}


class _Disk:
    def __init__(self, free: int) -> None:
        self.free = free

    def __call__(self, root: Path) -> int:
        return self.free


@pytest.fixture
def disk(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    probe = _Disk(free=1 << 40)
    monkeypatch.setattr(storage, "free_bytes", probe)
    _cleanup_all()
    try:
        yield probe
    finally:
        _cleanup_all()


def _sample(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


# --- the happy path ---------------------------------------------------------------------------


def test_one_request_opens_every_session_in_request_order(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client

    r = client.post(
        BATCH, json={"entries": [_entry("a.jpg", 3), _entry("b.jpg", 5), _entry("c.jpg", 7)]}
    )

    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["status"] for x in results] == [201, 201, 201]
    assert all("detail" not in x for x in results)  # absent, not null: no null arm for Swift
    uids = [x["upload"]["upload_uid"] for x in results]
    assert [x["upload"]["bytes_received"] for x in results] == [0, 0, 0]
    assert {x["upload"]["chunk_size"] for x in results} == {settings.UPLOAD_CHUNK_SIZE}
    assert _rows() == [(uids[0], "a.jpg", 3, 0), (uids[1], "b.jpg", 5, 0), (uids[2], "c.jpg", 7, 0)]
    assert _parts(tmp_path) == sorted(tmp_path / "uploads" / f"{u}.part" for u in uids)
    # Each session is an ordinary one: the single-session verbs reach it.
    head = client.head(f"/api/uploads/{uids[1]}")
    assert head.status_code == 200
    assert head.headers["Upload-Offset"] == "0"


def test_fifty_entries_is_the_largest_batch(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client

    r = client.post(BATCH, json={"entries": [_entry(f"{i}.jpg") for i in range(50)]})

    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 50
    assert sorted(row[1] for row in _rows()) == sorted(f"{i}.jpg" for i in range(50))


@pytest.mark.parametrize("count", [0, 51])
def test_an_empty_or_oversized_batch_is_refused_whole(logged_in_client, disk, tmp_path, count):
    client, _ = logged_in_client

    r = client.post(BATCH, json={"entries": [_entry(f"{i}.jpg") for i in range(count)]})

    assert r.status_code == 422, r.text
    assert _rows() == []
    assert _parts(tmp_path) == []


def test_an_invalid_entry_refuses_the_whole_request(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client

    r = client.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg", sha256="NOPE")]})

    assert r.status_code == 422, r.text
    assert _rows() == []


# --- per-entry refusals -----------------------------------------------------------------------


def test_an_entry_over_the_size_limit_is_refused_alone(
    logged_in_client, disk, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "MAX_UPLOAD_MIB", 1)
    limit = 1024 * 1024
    rejected = _sample("memosng_upload_sessions_total", {"result": "rejected_size"})

    r = client.post(
        BATCH,
        json={
            "entries": [_entry("a.jpg", limit), _entry("big.mov", limit + 1), _entry("c.jpg", 1)]
        },
    )

    assert r.status_code == 200, r.text
    first, refused, last = r.json()["results"]
    assert refused["status"] == 413
    assert "upload" not in refused
    assert "1 MiB" in refused["detail"]
    assert (first["status"], last["status"]) == (201, 201)
    assert [row[1] for row in _rows()] == ["a.jpg", "c.jpg"]
    assert len(_parts(tmp_path)) == 2
    # The same metric the single create bumps for its 413, once per refused entry.
    assert _sample("memosng_upload_sessions_total", {"result": "rejected_size"}) - rejected == 1


def test_disk_admission_counts_the_batch_and_refuses_only_what_does_not_fit(
    logged_in_client, disk, tmp_path
):
    client, _ = logged_in_client
    # 1000 + 1000 plus 10 % is exactly 2200; the 5000 in between never fits, and a refused entry
    # claims nothing, so the third still fits behind the first.
    disk.free = 2200

    r = client.post(
        BATCH,
        json={
            "entries": [_entry("a.mov", 1000), _entry("b.mov", 5000), _entry("c.mov", 1000)]
            + [_entry("d.mov", 1)]
        },
    )

    assert r.status_code == 200, r.text
    statuses = [x["status"] for x in r.json()["results"]]
    assert statuses == [201, 507, 201, 507]
    refused = r.json()["results"][1]
    assert "upload" not in refused and "space" in refused["detail"].lower()
    assert [row[1] for row in _rows()] == ["a.mov", "c.mov"]
    assert len(_parts(tmp_path)) == 2


def test_a_dedupe_hit_claims_no_space_in_the_batch(logged_in_client, disk, tmp_path):
    client, _ = logged_in_client
    _, twin = _ready_memo(client)
    original = Path(_att(twin)["storage_path"])
    disk.free = 1100  # room for 1000 bytes and nothing else that must travel

    r = client.post(
        BATCH,
        json={
            "entries": [
                _entry("same.png", len(ONE), sha256=_sha(ONE)),
                _entry("new.mov", 1000),
            ]
        },
    )

    assert r.status_code == 200, r.text
    hit, miss = r.json()["results"]
    assert hit["status"] == 201 and miss["status"] == 201
    assert hit["upload"]["bytes_received"] == len(ONE)  # born complete
    assert miss["upload"]["bytes_received"] == 0
    part = tmp_path / "uploads" / f"{hit['upload']['upload_uid']}.part"
    assert os.path.samefile(part, original)


# --- atomicity --------------------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", [1, 3])
def test_a_failure_part_way_leaves_no_session_and_no_part(
    logged_in_client, disk, tmp_path, monkeypatch, fail_at
):
    client, _ = logged_in_client
    _, twin = _ready_memo(client)
    original = Path(_att(twin)["storage_path"])
    _cleanup_uploads_only()
    real = svc.create_upload
    calls = 0

    async def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_at + 1:
            raise RuntimeError("boom")
        return await real(*args, **kwargs)

    monkeypatch.setattr(svc, "create_upload", flaky)

    r = client.post(
        BATCH,
        json={
            "entries": [
                _entry("same.png", len(ONE), sha256=_sha(ONE)),  # a hard-linked part
                _entry("b.jpg"),
                _entry("c.jpg"),
                _entry("d.jpg"),
                _entry("e.jpg"),
            ]
        },
    )

    assert r.status_code == 500
    assert calls == fail_at + 1
    assert _rows() == []
    assert _parts(tmp_path) == []
    assert original.read_bytes() == ONE  # unlinking the staged name left the original alone


def _cleanup_uploads_only() -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


# --- auth -------------------------------------------------------------------------------------


def test_an_anonymous_batch_is_refused_with_401(client, disk, tmp_path):
    r = client.post(BATCH, json={"entries": [_entry("a.jpg")]})

    assert r.status_code == 401, r.text
    assert _rows() == []


async def test_the_capture_grant_can_batch(pat_client, disk, tmp_path):
    bearer, subject, _ = pat_client
    minted = await bearer.post("/api/tokens/capture-grant")
    assert minted.status_code == 201, minted.text
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {minted.json()['token']}"},
    ) as grant:
        r = await grant.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})

    assert r.status_code == 200, r.text
    assert [x["status"] for x in r.json()["results"]] == [201, 201]
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        owners = c.execute(
            text("SELECT DISTINCT u.subject FROM uploads up JOIN users u ON u.id = up.owner_id")
        ).fetchall()
    eng.dispose()
    assert [o.subject for o in owners] == [subject]


async def test_two_batches_at_once_each_open_their_own_sessions(pat_client, disk, tmp_path):
    bearer, _, _ = pat_client

    first, second = await asyncio.gather(
        bearer.post(BATCH, json={"entries": [_entry(f"x{i}.jpg") for i in range(3)]}),
        bearer.post(BATCH, json={"entries": [_entry(f"y{i}.jpg") for i in range(3)]}),
    )

    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    uids = [x["upload"]["upload_uid"] for r in (first, second) for x in r.json()["results"]]
    assert len(set(uids)) == 6
    by_uid = {row[0]: row[1] for row in _rows()}
    assert [by_uid[u] for u in uids] == [f"x{i}.jpg" for i in range(3)] + [
        f"y{i}.jpg" for i in range(3)
    ]
    assert len(_parts(tmp_path)) == 6


# --- the schema -------------------------------------------------------------------------------


def _has_null_arm(node: dict) -> bool:
    return any(branch.get("type") == "null" for branch in node.get("anyOf", []))


def test_the_schema_bounds_the_batch_and_carries_no_null_arm():
    doc = app.openapi()
    op = doc["paths"][BATCH]["post"]
    assert set(op["responses"]) >= {"200", "422"}
    schemas = doc["components"]["schemas"]
    entries = schemas["UploadBatchCreate"]["properties"]["entries"]
    assert (entries["minItems"], entries["maxItems"]) == (1, 50)
    assert entries["items"] == {"$ref": "#/components/schemas/UploadCreate"}
    result = schemas["UploadBatchResult"]
    assert result["required"] == ["status"]
    assert result["properties"]["upload"]["$ref"] == "#/components/schemas/UploadOut"
    for name, prop in result["properties"].items():
        assert not _has_null_arm(prop), name
