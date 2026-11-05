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
import contextlib
import os
import signal
import time
from pathlib import Path

import pytest
from app.config import settings
from app.main import app
from app.models import Upload, User
from app.services import storage
from app.services import uploads as svc
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import SessionTransaction

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


# --- exact part cleanup (MEMNG-1182) ------------------------------------------------------------
#
# A part is the cleanup's from the moment it exists, not from the moment its entry succeeded: a
# failure between the two used to leave it on disk with no row to find it by. And a COMMIT that
# raises does not prove a rollback: only what a fresh read shows did not land is unlinked.


def _holds_upload(session) -> bool:
    return any(isinstance(obj, Upload) for obj in session.identity_map.values())


@pytest.mark.parametrize("fail_at", [1, 2])
def test_a_failed_savepoint_release_leaves_no_orphan_part(
    logged_in_client, disk, tmp_path, monkeypatch, fail_at
):
    client, _ = logged_in_client
    real = SessionTransaction.commit
    releases = 0

    def flaky(self, *args, **kwargs):
        nonlocal releases
        if self.nested and _holds_upload(self.session):
            releases += 1
            if releases == fail_at:  # the entry's part is on disk by now
                raise RuntimeError("savepoint release failed")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SessionTransaction, "commit", flaky)

    r = client.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg"), _entry("c.jpg")]})

    assert r.status_code == 500
    assert releases == fail_at
    assert _rows() == []
    assert _parts(tmp_path) == []


def test_a_failed_flush_after_the_staging_link_leaves_no_orphan_link(
    logged_in_client, disk, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    _, twin = _ready_memo(client)
    original = Path(_att(twin)["storage_path"])
    _cleanup_uploads_only()
    real = AsyncSession.flush
    failed = 0

    async def flaky(self, *args, **kwargs):
        nonlocal failed
        # The hit's second flush, the one recording its checked digest — after the link exists.
        if any(isinstance(obj, Upload) and obj.sha256 for obj in self.dirty):
            failed += 1
            raise RuntimeError("flush failed")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flaky)

    r = client.post(
        BATCH,
        json={"entries": [_entry("a.jpg"), _entry("same.png", len(ONE), sha256=_sha(ONE))]},
    )

    assert r.status_code == 500
    assert failed == 1
    assert _rows() == []
    assert _parts(tmp_path) == []
    assert original.read_bytes() == ONE  # only the staged name went


def _commit_that(monkeypatch, *, lands: bool, then: BaseException) -> None:
    """Make the batch's COMMIT raise `then` — after the database accepted it, or instead of it."""
    real = AsyncSession.commit

    async def commit(self):
        if not _holds_upload(self.sync_session):
            return await real(self)
        if lands:
            await real(self)
        raise then

    monkeypatch.setattr(AsyncSession, "commit", commit)


def _part_names(data_root: Path) -> set[str]:
    return {p.name for p in _parts(data_root)}


def test_a_commit_that_landed_before_it_raised_keeps_its_parts(
    logged_in_client, disk, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    _, twin = _ready_memo(client)
    original = Path(_att(twin)["storage_path"])
    _cleanup_uploads_only()
    _commit_that(monkeypatch, lands=True, then=ConnectionResetError("lost after COMMIT"))

    r = client.post(
        BATCH,
        json={"entries": [_entry("a.jpg"), _entry("same.png", len(ONE), sha256=_sha(ONE))]},
    )

    assert r.status_code == 500
    rows = _rows()
    assert [row[1] for row in rows] == ["a.jpg", "same.png"]  # the sessions exist...
    assert _part_names(tmp_path) == {f"{row[0]}.part" for row in rows}  # ...and so do their parts
    hit = next(row[0] for row in rows if row[1] == "same.png")
    assert os.path.samefile(tmp_path / "uploads" / f"{hit}.part", original)


def test_a_commit_that_never_landed_still_cleans_up(logged_in_client, disk, tmp_path, monkeypatch):
    client, _ = logged_in_client
    _, twin = _ready_memo(client)
    original = Path(_att(twin)["storage_path"])
    _cleanup_uploads_only()
    _commit_that(monkeypatch, lands=False, then=ConnectionResetError("lost before COMMIT"))

    r = client.post(
        BATCH,
        json={"entries": [_entry("a.jpg"), _entry("same.png", len(ONE), sha256=_sha(ONE))]},
    )

    assert r.status_code == 500
    assert _rows() == []
    assert _parts(tmp_path) == []
    assert original.read_bytes() == ONE


def test_an_unresolvable_commit_outcome_unlinks_nothing(
    logged_in_client, disk, tmp_path, monkeypatch
):
    client, _ = logged_in_client
    _commit_that(monkeypatch, lands=False, then=ConnectionResetError("lost during COMMIT"))

    def unreachable():
        raise ConnectionRefusedError("database unreachable")

    monkeypatch.setattr(svc, "async_session", unreachable)

    r = client.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})

    assert r.status_code == 500
    # Nothing landed here, but the handler cannot know that: a part it cannot prove orphaned stays.
    assert len(_parts(tmp_path)) == 2


@pytest.mark.parametrize("lands", [True, False])
@pytest.mark.parametrize("cancels", [1, 3])
async def test_a_request_cancelled_in_its_commit_unlinks_only_what_did_not_land(
    pat_client, disk, tmp_path, monkeypatch, lands, cancels
):
    bearer, _, _ = pat_client
    real = AsyncSession.commit
    parked = asyncio.Event()

    async def commit(self):
        if not _holds_upload(self.sync_session):
            return await real(self)
        if lands:
            await real(self)
        parked.set()
        await asyncio.Event().wait()  # cancelled here: inside the COMMIT, after or before it ran

    monkeypatch.setattr(AsyncSession, "commit", commit)
    request = asyncio.create_task(
        bearer.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})
    )
    await asyncio.wait_for(parked.wait(), timeout=10)
    # More than one is the shape a level-triggered cancel scope delivers: the resolution's re-read
    # must survive the next one too.
    for _ in range(cancels):
        request.cancel()
        await asyncio.sleep(0.05)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=10)

    assert request.cancelled(), "the batch swallowed a cancellation its caller has to see"
    rows = _rows()
    if lands:
        assert len(rows) == 2
        assert _part_names(tmp_path) == {f"{row[0]}.part" for row in rows}
    else:
        assert rows == []
        assert _parts(tmp_path) == []


class _ParkedSessions:
    """`svc.async_session` that holds each fresh session until released — the outcome read parks."""

    def __init__(self, real) -> None:
        self.real = real
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self):
        parked = self

        class _Ctx:
            async def __aenter__(self):
                parked.entered.set()
                await parked.release.wait()
                self.inner = parked.real()
                return await self.inner.__aenter__()

            async def __aexit__(self, *exc):
                return await self.inner.__aexit__(*exc)

        return _Ctx()


async def _settle() -> None:
    """Let what a failed request left behind (its session's shielded close) finish before the
    fixture's synchronous cleanup blocks this loop — or that cleanup waits on it forever."""
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=5)


async def test_an_outcome_read_that_never_answers_gives_up_and_unlinks_nothing(
    pat_client, disk, tmp_path, monkeypatch
):
    bearer, _, _ = pat_client
    _commit_that(monkeypatch, lands=False, then=ConnectionResetError("lost during COMMIT"))
    parked = _ParkedSessions(svc.async_session)
    monkeypatch.setattr(svc, "async_session", parked)
    monkeypatch.setattr(svc, "COMMIT_OUTCOME_TIMEOUT_S", 0.2)
    # Released long after the bound: an unbounded read would wait for it, then answer "nothing
    # landed" and unlink — so a missing bound fails below instead of hanging the suite.
    asyncio.get_running_loop().call_later(3, parked.release.set)

    started = time.monotonic()
    # The in-process transport re-raises what the app raised: the COMMIT's own error.
    with pytest.raises(ConnectionResetError):
        await asyncio.wait_for(
            bearer.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]}), timeout=10
        )

    assert time.monotonic() - started < 2, "the outcome read outlived its bound"

    assert len(_parts(tmp_path)) == 2  # unknown: kept for the operator, not guessed away


async def test_a_cancellation_during_the_outcome_read_waits_for_it_and_is_still_delivered(
    pat_client, disk, tmp_path, monkeypatch
):
    bearer, _, _ = pat_client
    _commit_that(monkeypatch, lands=False, then=ConnectionResetError("lost before COMMIT"))
    parked = _ParkedSessions(svc.async_session)
    monkeypatch.setattr(svc, "async_session", parked)
    request = asyncio.create_task(
        bearer.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})
    )
    try:
        await asyncio.wait_for(parked.entered.wait(), timeout=10)
        for _ in range(3):  # level-triggered: each re-await of the read survives the next one
            request.cancel()
            await asyncio.sleep(0.05)
        assert not request.done(), "the request unwound before it knew what its COMMIT did"
        parked.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=10)
    finally:
        parked.release.set()
        await _settle()

    assert request.cancelled(), "the cancellation that arrived during the read was swallowed"
    assert _rows() == []
    assert _parts(tmp_path) == []  # the read answered "nothing landed", and that was acted on


class _LoopBlocked(Exception):
    pass


@contextlib.contextmanager
def _loop_watchdog(seconds: float):
    """Fail, instead of hanging the suite, when the event loop stops yielding: a SIGALRM handler
    runs between bytecodes on the main thread — the loop's — and raises out of a busy coroutine."""

    def blocked(signum, frame):
        raise _LoopBlocked(f"the event loop did not yield for {seconds}s")

    previous = signal.signal(signal.SIGALRM, blocked)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _outcome_read_task() -> asyncio.Task:
    (task,) = [t for t in asyncio.all_tasks() if "landed_upload_uids" in t.get_coro().__qualname__]
    return task


@pytest.mark.parametrize("request_too", [False, True])
async def test_a_cancelled_outcome_read_answers_unknown_instead_of_spinning(
    pat_client, disk, tmp_path, monkeypatch, request_too
):
    """A shutdown cancels every task — the outcome read's own included. Awaiting a cancelled task
    raises at once without yielding, so a loop that only remembers the cancellation spins forever
    and blocks the event loop. The read must answer "unknown" and keep the caller's cancellation."""
    bearer, _, _ = pat_client
    _commit_that(monkeypatch, lands=False, then=ConnectionResetError("lost before COMMIT"))
    parked = _ParkedSessions(svc.async_session)
    monkeypatch.setattr(svc, "async_session", parked)
    request = asyncio.create_task(
        bearer.post(BATCH, json={"entries": [_entry("a.jpg"), _entry("b.jpg")]})
    )
    outcome: BaseException | None = None
    try:
        await asyncio.wait_for(parked.entered.wait(), timeout=10)
        with _loop_watchdog(3):
            _outcome_read_task().cancel()
            if request_too:  # the same tick: the request's own cancellation must survive
                request.cancel()
            try:
                await asyncio.wait_for(request, timeout=10)
            except BaseException as exc:
                outcome = exc
    finally:
        parked.release.set()
        await _settle()

    assert not isinstance(outcome, _LoopBlocked), outcome
    if request_too:
        assert request.cancelled(), f"the request's cancellation was swallowed: {outcome!r}"
    else:
        assert isinstance(outcome, ConnectionResetError), outcome  # the COMMIT's own error
    assert len(_parts(tmp_path)) == 2  # unknown: nothing unlinked


async def test_create_upload_registers_the_uid_before_its_part_exists(
    disk, adb, tmp_path, monkeypatch
):
    owner = User(subject="part-registry", role="user")
    adb.add(owner)
    await adb.flush()
    real = svc._create_part_sync

    def made_then_failed(path: Path) -> None:
        real(path)
        raise OSError("failed after the part was made")

    monkeypatch.setattr(svc, "_create_part_sync", made_then_failed)
    registered: list[str] = []

    with pytest.raises(OSError):
        await svc.create_upload(
            adb,
            owner_id=owner.id,
            filename="a.jpg",
            mime="image/jpeg",
            size=3,
            part_uids_out=registered,
        )

    assert [svc.part_path(u) for u in registered] == _parts(tmp_path)
    assert len(registered) == 1
    # Without the list, the single create and every other caller behave exactly as before.
    monkeypatch.setattr(svc, "_create_part_sync", real)
    upload = await svc.create_upload(
        adb, owner_id=owner.id, filename="b.jpg", mime="image/jpeg", size=3
    )
    assert svc.part_path(upload.upload_uid).exists()
    assert len(registered) == 1


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
