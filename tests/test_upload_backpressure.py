"""Backpressure on the upload body path and on the connection pool.

A PATCH to /api/uploads/{uid} holds its request for as long as the client takes to send the bytes,
so a burst of slow uploads is the one load the server cannot shed by answering quickly. Each user
may stream a few bodies at once and the process a few more in total; a request over either cap is
answered 429 with a Retry-After before its body is read. A request that cannot get a pooled
connection in time is answered 503 with a Retry-After instead of a 500.
"""

import asyncio
import json

import pytest
import sqlalchemy.exc
from app import auth
from app import database as app_database
from app.config import settings
from app.csrf import canonical_origin
from app.main import app
from app.routers import uploads as uploads_router
from structlog.testing import capture_logs

from tests.test_uploads import _cleanup, _cookie_client, _drop_user, _login_as, _start

HALF = 64
SIZE = 2 * HALF
UPLOAD_PATH = "/api/uploads/{upload_uid}"


class _Held:
    """A request body that stops after its first half until released: while it waits, the route is
    inside the body, i.e. past admission and holding its permit."""

    def __init__(self) -> None:
        self.inside = asyncio.Event()
        self.go = asyncio.Event()

    async def body(self):
        yield b"a" * HALF
        self.inside.set()
        await self.go.wait()
        yield b"b" * HALF


async def _hold(ac, uids: list[str]) -> tuple[list[_Held], list[asyncio.Task]]:
    helds = [_Held() for _ in uids]
    tasks = [
        asyncio.create_task(
            ac.patch(f"/api/uploads/{uid}", content=h.body(), headers={"Upload-Offset": "0"})
        )
        for uid, h in zip(uids, helds, strict=True)
    ]
    await asyncio.wait_for(asyncio.gather(*(h.inside.wait() for h in helds)), timeout=10)
    return helds, tasks


async def _release(helds: list[_Held], tasks: list[asyncio.Task]) -> list:
    for h in helds:
        h.go.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


def _statuses(responses: list) -> list[int | str]:
    return [r.status_code if hasattr(r, "status_code") else repr(r) for r in responses]


def _sid(client) -> str:
    return [c.value for c in client.cookies.jar if c.name == auth.SID_COOKIE][-1] or ""


async def _raw_patch(sid: str, uid: str, offset: int, receive, content_length: int):
    """PATCH through the complete app with a caller-supplied `receive`, so a test can cut the body
    the way uvicorn does (`http.disconnect`) or count how often the application asked for it."""
    headers = [
        (b"host", b"testserver"),
        (b"cookie", f"{auth.SID_COOKIE}={sid}".encode()),
        (b"origin", canonical_origin().encode()),
        (b"upload-offset", str(offset).encode()),
        (b"content-type", b"application/octet-stream"),
        (b"content-length", str(content_length).encode()),
    ]
    path = f"/api/uploads/{uid}"
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
    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    head = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], head, json.loads(body)


def _admission_is_idle() -> bool:
    return uploads_router.body_admission.total == 0 and uploads_router.body_admission.per_user == {}


async def test_a_fourth_concurrent_body_of_one_user_is_refused_with_retry_after(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uids = [_start(client, f"h{i}.bin", "application/octet-stream", SIZE) for i in range(4)]
        async with _cookie_client(client) as ac:
            helds, tasks = await _hold(ac, uids[:3])
            try:
                refused = await ac.patch(
                    f"/api/uploads/{uids[3]}", content=b"x" * SIZE, headers={"Upload-Offset": "0"}
                )
            finally:
                held = await _release(helds, tasks)
            assert refused.status_code == 429, refused.text
            assert refused.headers["Retry-After"] == "2"
            assert _statuses(held) == [200, 200, 200]
            assert [r.json()["bytes_received"] for r in held] == [SIZE] * 3
            assert (await ac.head(f"/api/uploads/{uids[3]}")).headers["Upload-Offset"] == "0"
            assert _admission_is_idle()
            # once the three are done the same request is let in
            again = await ac.patch(
                f"/api/uploads/{uids[3]}", content=b"x" * SIZE, headers={"Upload-Offset": "0"}
            )
            assert again.status_code == 200, again.text
    finally:
        _cleanup()


async def test_another_user_is_admitted_while_one_user_is_at_the_cap(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    other = _login_as(monkeypatch, "backpressure_user_b")
    try:
        mine = [_start(client, f"a{i}.bin", "application/octet-stream", SIZE) for i in range(3)]
        theirs = _start(other, "b.bin", "application/octet-stream", SIZE)
        async with _cookie_client(client) as ac, _cookie_client(other) as bc:
            helds, tasks = await _hold(ac, mine)
            try:
                admitted = await bc.patch(
                    f"/api/uploads/{theirs}", content=b"y" * SIZE, headers={"Upload-Offset": "0"}
                )
            finally:
                held = await _release(helds, tasks)
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["bytes_received"] == SIZE
        assert _statuses(held) == [200, 200, 200]
        assert _admission_is_idle()
    finally:
        _cleanup()
        _drop_user("backpressure_user_b")


async def test_a_third_user_is_refused_once_the_process_holds_its_cap(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    second = _login_as(monkeypatch, "backpressure_user_b")
    third = _login_as(monkeypatch, "backpressure_user_c")
    try:
        a = [_start(client, f"a{i}.bin", "application/octet-stream", SIZE) for i in range(3)]
        b = [_start(second, f"b{i}.bin", "application/octet-stream", SIZE) for i in range(3)]
        c = _start(third, "c.bin", "application/octet-stream", SIZE)
        async with (
            _cookie_client(client) as ac,
            _cookie_client(second) as bc,
            _cookie_client(third) as cc,
        ):
            held_a, tasks_a = await _hold(ac, a)
            held_b, tasks_b = await _hold(bc, b)
            try:
                refused = await cc.patch(
                    f"/api/uploads/{c}", content=b"z" * SIZE, headers={"Upload-Offset": "0"}
                )
            finally:
                held = await _release(held_a + held_b, tasks_a + tasks_b)
            assert refused.status_code == 429, refused.text
            assert refused.headers["Retry-After"] == "2"
            assert _statuses(held) == [200] * 6
            assert _admission_is_idle()
            again = await cc.patch(
                f"/api/uploads/{c}", content=b"z" * SIZE, headers={"Upload-Offset": "0"}
            )
            assert again.status_code == 200, again.text
    finally:
        _cleanup()
        _drop_user("backpressure_user_b")
        _drop_user("backpressure_user_c")


async def test_a_body_cut_mid_stream_gives_its_permit_back(logged_in_client, monkeypatch, tmp_path):
    # One body per user, so a permit the cut request failed to return would refuse the resume.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 1)
    content = b"c" * 4000
    try:
        uid = _start(client, "cut.bin", "application/octet-stream", len(content))
        pieces = [{"type": "http.request", "body": content[:2500], "more_body": True}]
        held_during_body: list[int] = []

        async def receive():
            held_during_body.append(uploads_router.body_admission.total)
            return pieces.pop(0) if pieces else {"type": "http.disconnect"}

        status, _, out = await _raw_patch(_sid(client), uid, 0, receive, len(content))
        assert status == 200
        assert out["bytes_received"] == 2500
        assert held_during_body and set(held_during_body) == {1}  # admitted while it streamed
        assert _admission_is_idle()
        async with _cookie_client(client) as ac:
            resumed = await ac.patch(
                f"/api/uploads/{uid}", content=content[2500:], headers={"Upload-Offset": "2500"}
            )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["bytes_received"] == len(content)
    finally:
        _cleanup()


async def test_a_refused_body_is_never_read(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uids = [_start(client, f"r{i}.bin", "application/octet-stream", SIZE) for i in range(4)]
        reads = 0

        async def receive():
            nonlocal reads
            reads += 1
            return {"type": "http.request", "body": b"x" * SIZE, "more_body": False}

        async with _cookie_client(client) as ac:
            helds, tasks = await _hold(ac, uids[:3])
            try:
                status, head, out = await _raw_patch(_sid(client), uids[3], 0, receive, SIZE)
            finally:
                held = await _release(helds, tasks)
        assert (status, head.get("retry-after")) == (429, "2"), out
        assert reads == 0
        assert _statuses(held) == [200, 200, 200]
    finally:
        _cleanup()


async def test_four_bodies_arriving_together_admit_exactly_three(
    logged_in_client, monkeypatch, tmp_path
):
    # All four race through authentication and the lookup at once, so an await between the cap
    # check and the increment would let every one of them past the check.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # Line the four up after their lookups, so they reach admission in the same few loop ticks
    # instead of spread out by their own database round trips.
    lookup = uploads_router._get_upload
    arrived: list[str] = []
    lined_up = asyncio.Event()

    async def lookup_then_line_up(db, upload_uid, owner_id):
        upload = await lookup(db, upload_uid, owner_id)
        arrived.append(upload_uid)
        if len(arrived) == 4:
            lined_up.set()
        await asyncio.wait_for(lined_up.wait(), timeout=10)
        return upload

    try:
        uids = [_start(client, f"t{i}.bin", "application/octet-stream", SIZE) for i in range(4)]
        monkeypatch.setattr(uploads_router, "_get_upload", lookup_then_line_up)
        helds = [_Held() for _ in uids]
        async with _cookie_client(client) as ac:
            tasks = [
                asyncio.create_task(
                    ac.patch(
                        f"/api/uploads/{uid}", content=h.body(), headers={"Upload-Offset": "0"}
                    )
                )
                for uid, h in zip(uids, helds, strict=True)
            ]
            try:
                # Settled: the refused one has answered and the admitted ones are inside the body.
                for _ in range(1000):
                    done = sum(t.done() for t in tasks)
                    inside = sum(h.inside.is_set() for h in helds)
                    if done + inside >= 4:
                        break
                    await asyncio.sleep(0.01)
            finally:
                results = await _release(helds, tasks)
        assert (done, inside) == (1, 3)
        assert sorted(_statuses(results), key=str) == [200, 200, 200, 429]
        assert _admission_is_idle()
    finally:
        _cleanup()


async def test_a_refused_offset_gives_its_permit_back(logged_in_client, monkeypatch, tmp_path):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_BODIES_PER_USER", 1)
    try:
        uid = _start(client, "gap.bin", "application/octet-stream", SIZE)
        async with _cookie_client(client) as ac:
            gap = await ac.patch(
                f"/api/uploads/{uid}", content=b"g" * HALF, headers={"Upload-Offset": "7"}
            )
            assert gap.status_code == 409, gap.text
            assert _admission_is_idle()
            ok = await ac.patch(
                f"/api/uploads/{uid}", content=b"g" * SIZE, headers={"Upload-Offset": "0"}
            )
        assert ok.status_code == 200, ok.text
    finally:
        _cleanup()


async def test_a_user_at_the_cap_can_still_read_and_start_uploads(
    logged_in_client, monkeypatch, tmp_path
):
    # The cap is on streaming bodies only: the offset HEAD, a new session and the feed stay open.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        uids = [_start(client, f"s{i}.bin", "application/octet-stream", SIZE) for i in range(3)]
        async with _cookie_client(client) as ac:
            helds, tasks = await _hold(ac, uids)
            try:
                head = await ac.head(f"/api/uploads/{uids[0]}")
                started = await ac.post(
                    "/api/uploads",
                    json={"filename": "n.bin", "mime": "application/octet-stream", "size": 1},
                )
                feed = await ac.get("/api/memos")
            finally:
                held = await _release(helds, tasks)
        assert (head.status_code, started.status_code, feed.status_code) == (200, 201, 200)
        assert _statuses(held) == [200, 200, 200]
    finally:
        _cleanup()


def test_a_pool_timeout_is_answered_503_with_retry_after(logged_in_client, monkeypatch):
    client, _ = logged_in_client

    class _Exhausted:
        async def __aenter__(self):
            raise sqlalchemy.exc.TimeoutError("QueuePool limit reached, connection timed out")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(app_database, "async_session", lambda: _Exhausted())
    with capture_logs() as logs:
        r = client.get("/api/memos")
    assert r.status_code == 503, r.text
    assert r.headers["Retry-After"] == "5"
    assert r.json() == {"detail": "Database busy; retry later"}
    busy = [e for e in logs if e["event"] == "db.pool_exhausted"]
    assert busy == [{"event": "db.pool_exhausted", "path": "/api/memos", "log_level": "warning"}]


def _resolve(doc: dict, node: dict) -> dict:
    while "$ref" in node:
        target: dict = doc
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def _operations(doc: dict):
    for path, item in doc["paths"].items():
        for method, operation in item.items():
            if method in ("get", "put", "post", "patch", "delete", "head"):
                yield method, path, operation


def test_the_upload_patch_publishes_both_refusals_with_retry_after():
    doc = app.openapi()
    responses = doc["paths"][UPLOAD_PATH]["patch"]["responses"]
    for status in ("429", "503"):
        response = _resolve(doc, responses[status])
        assert response["description"]
        assert response["headers"]["Retry-After"]["schema"] == {"type": "integer"}


def test_the_typed_memo_503s_keep_their_own_schemas():
    doc = app.openapi()
    for path, method, schema in (
        ("/api/memos", "post", "CreateRacedError"),
        ("/api/memos/{uid}", "patch", "EditRacedError"),
    ):
        response = doc["paths"][path][method]["responses"]["503"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{schema}"
        }
        assert "headers" not in response


@pytest.mark.parametrize("method", ["get", "head"])
def test_every_operation_publishes_the_pool_503(method):
    doc = app.openapi()
    operations = [(p, op) for m, p, op in _operations(doc) if m == method]
    assert operations
    for path, operation in operations:
        response = _resolve(doc, operation["responses"]["503"])
        assert response["headers"]["Retry-After"]["schema"] == {"type": "integer"}, path
        if method == "head":  # a HEAD answer has no body to describe
            assert "content" not in response, path


def test_path_level_keys_are_not_mistaken_for_operations():
    from app.main import attach_database_busy_responses

    schema = {
        "paths": {
            "/x": {
                "summary": "not an operation",
                "parameters": [],
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        }
    }
    attach_database_busy_responses(schema)
    item = schema["paths"]["/x"]
    assert item["summary"] == "not an operation" and item["parameters"] == []
    assert item["get"]["responses"]["503"] == {"$ref": "#/components/responses/DatabaseBusy"}


def test_no_operation_is_left_without_a_503():
    doc = app.openapi()
    assert [(m, p) for m, p, op in _operations(doc) if "503" not in op["responses"]] == []
