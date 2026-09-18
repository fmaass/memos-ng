"""MEMNG-31: a PAT used on the compat surface leaves last_used durably updated.
MEMNG-81: and it does so WITHOUT the request ever waiting on a second pool checkout.

Before MEMNG-31, `auth.authenticate_pat` only FLUSHED the stamp, and the compat surface never
commits (routers/compat.py `optional_user`, and `serve_file_compat`'s own short-lived session) — so
the transaction was rolled back at request end and the row never changed. The stamp therefore lives
in its own session (`auth.stamp_pat_last_used`), throttled to one write per window per PAT.

Its own session means a SECOND connection, though, and until MEMNG-81 the request awaited that
write while still holding its own pooled connection: hold-and-wait, the cycle a saturated pool can
deadlock on. The write is now handed to a background task by `auth.schedule_pat_last_used_stamp`
(claim taken synchronously first, so a burst still collapses to one attempt) and the request
returns — releasing its connection — while the task queues for a slot.

Every read-back here opens a FRESH engine + connection, so it can only observe COMMITTED state —
that is what makes these tests fail if the commit goes away. The stamp is no longer finished when
the response is, so each read-back is preceded by `_drain()`, which AWAITS the scheduled task in
the app's own event loop (never a sleep — see the `pat_client` fixture for why the plain
TestClient cannot be used for this).
"""

import ast
import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app import auth
from app.config import settings
from app.database import async_session
from app.models import Pat
from PIL import Image
from sqlalchemy import create_engine, select, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

SUBJECT = "patlu_owner"
TOKEN = "memos_pat_patlastused0000000000"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _clear_claims():
    # The in-process claim map is module state shared by the whole process; start each test from a
    # clean one so ordering can't decide whether a stamp is attempted.
    auth._pat_last_used_claimed.clear()
    yield
    auth._pat_last_used_claimed.clear()


@pytest.fixture
def pat_client(client, monkeypatch, tmp_path):
    """A TestClient whose event loop OUTLIVES the individual request, plus `_drain()`.

    Determinism, not politeness: the stamp is now a background task on the app's loop, and a plain
    TestClient starts a blocking portal PER REQUEST and tears it down the moment the ASGI call
    returns — `asyncio.run` then cancels whatever is still pending, so a task needing more than a
    loop hop (a DB round-trip needs many) would never run at all. Entering the client keeps one
    loop alive across requests, which is what lets `_drain()` await the scheduled write instead of
    sleeping and hoping.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))  # lifespan creates its data dirs
    with client:
        yield client


def _drain(pat_client) -> None:
    """Await every scheduled stamp task inside the app's loop (the sleep-free read-back gate)."""
    assert pat_client.portal is not None, "fixture must keep the portal open"
    pat_client.portal.call(auth.drain_pat_stamp_tasks)


def _seed(storage_path: str | None = None) -> int:
    """One user + PAT + a PUBLIC memo with an image attachment. Returns the pat id."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        uid = c.execute(
            text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
            {"s": SUBJECT},
        ).scalar_one()
        pat_id = c.execute(
            text(
                "INSERT INTO pats (hashed_token, name, user_id) VALUES (:h,'sat',:u) RETURNING id"
            ),
            {"h": hashlib.sha256(TOKEN.encode()).hexdigest(), "u": uid},
        ).scalar_one()
        memo = c.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
                "VALUES ('patlu-memo',:o,'media memo',CAST('PUBLIC' AS visibility),false) "
                "RETURNING id"
            ),
            {"o": uid},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
                "storage_path) VALUES ('patlu-img',:o,:m,'IMG.jpeg','image/jpeg',1,"
                "CAST('ready' AS attachment_status),:sp)"
            ),
            {"o": uid, "m": memo, "sp": storage_path},
        )
    eng.dispose()
    return pat_id


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": SUBJECT})  # cascades
    eng.dispose()


def _last_used(pat_id: int) -> datetime | None:
    """AC2: read the row back through a FRESH engine/connection — uncommitted work is invisible."""
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        sql = text("SELECT last_used FROM pats WHERE id=:i")
        value = c.execute(sql, {"i": pat_id}).scalar_one()
    eng.dispose()
    return value


def _thumb(tmp_path):
    p = tmp_path / "assets" / "thumbs" / "patlu-img.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(p, "JPEG")


def test_compat_list_request_persists_last_used(pat_client, db_engine):
    """AC1 + AC2 on /api/v1/memos: the request-scoped session is never committed by the router."""
    pat_id = _seed()
    try:
        assert _last_used(pat_id) is None
        before = datetime.now(UTC)
        assert pat_client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        _drain(pat_client)
        stamped = _last_used(pat_id)
        assert stamped is not None
        assert stamped >= before - timedelta(seconds=5)
    finally:
        _cleanup()


def test_compat_file_request_persists_last_used(pat_client, db_engine, tmp_path):
    """AC1 + AC2 on /file/*: that route authorizes in its OWN session, which is closed (→ rolled
    back) before the file is streamed — the stamp has to carry its own commit or it is lost."""
    _thumb(tmp_path)
    pat_id = _seed()
    try:
        assert _last_used(pat_id) is None
        r = pat_client.get("/file/attachments/patlu-img/IMG.jpeg?thumbnail=true", headers=HEADERS)
        assert r.status_code == 200
        _drain(pat_client)
        assert _last_used(pat_id) is not None
    finally:
        _cleanup()


def test_clean_api_pat_request_still_persists_last_used(pat_client, db_engine, tmp_path):
    """The clean API's PAT path (require_pat_or_user → /api/attachments/{uid}/original) keeps
    persisting the stamp now that its own commit is gone and the scheduled task owns durability."""
    orig = tmp_path / "orig.jpg"
    Image.new("RGB", (12, 12), (4, 5, 6)).save(orig, "JPEG")
    pat_id = _seed(storage_path=str(orig))
    try:
        assert _last_used(pat_id) is None
        r = pat_client.get("/api/attachments/patlu-img/original", headers=HEADERS)
        assert r.status_code == 200
        _drain(pat_client)
        assert _last_used(pat_id) is not None
    finally:
        _cleanup()


def test_second_request_inside_the_window_does_not_rewrite(pat_client, db_engine, monkeypatch):
    """Throttle: one write per window per PAT, then a fresh write once the window has passed."""
    pat_id = _seed()
    try:
        assert pat_client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        _drain(pat_client)
        first = _last_used(pat_id)
        assert first is not None

        assert pat_client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        _drain(pat_client)
        assert _last_used(pat_id) == first  # inside the window: the row is untouched

        monkeypatch.setattr(settings, "PAT_LAST_USED_THROTTLE_S", 0)  # window elapsed
        assert pat_client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        _drain(pat_client)
        second = _last_used(pat_id)
        assert second is not None and second > first
    finally:
        _cleanup()


def test_a_process_without_the_in_process_claim_still_does_not_rewrite(pat_client, db_engine):
    """The claim map is per-process and non-durable (a second worker, a restart, or the >4096
    bound clearing it). The durable gates — the freshness check on the loaded row and the same
    predicate in the UPDATE's WHERE — must then hold the throttle on their own."""
    pat_id = _seed()
    try:
        assert pat_client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        _drain(pat_client)
        first = _last_used(pat_id)
        assert first is not None
        auth._pat_last_used_claimed.clear()  # a fresh process has no claim for this PAT
        assert pat_client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        _drain(pat_client)
        assert _last_used(pat_id) == first
    finally:
        _cleanup()


def test_pat_stamp_due_window():
    now = datetime.now(UTC)
    window = settings.PAT_LAST_USED_THROTTLE_S
    assert auth.pat_stamp_due(None, now) is True  # never used → always worth stamping
    assert auth.pat_stamp_due(now - timedelta(seconds=1), now) is False
    assert auth.pat_stamp_due(now - timedelta(seconds=window + 1), now) is True


def test_claim_pat_stamp_admits_one_caller_per_window():
    window = settings.PAT_LAST_USED_THROTTLE_S
    assert auth.claim_pat_stamp(4242, 1000.0) is True
    assert auth.claim_pat_stamp(4242, 1000.0 + window - 1) is False  # burst collapses here
    assert auth.claim_pat_stamp(4243, 1000.0) is True  # a different PAT is unaffected
    assert auth.claim_pat_stamp(4242, 1000.0 + window) is True


async def test_concurrent_burst_schedules_exactly_one_write(db_engine):
    """The folded MEMNG-25 finding: a gallery burst on one PAT must not have every request take
    that row's lock. The claim is still made synchronously — now before the task is created — so
    exactly one member of the burst schedules a write and the rest schedule nothing at all."""
    pat_id = _seed()
    try:

        async def one() -> asyncio.Task[None] | None:
            async with async_session() as db:
                pat = (await db.execute(select(Pat).where(Pat.id == pat_id))).scalar_one()
                return auth.schedule_pat_last_used_stamp(pat.id, pat.last_used)

        scheduled = [t for t in await asyncio.gather(*(one() for _ in range(8))) if t is not None]
        assert len(scheduled) == 1
        await asyncio.gather(*scheduled)
        assert _last_used(pat_id) is not None  # and the one write is committed
    finally:
        _cleanup()


def test_first_claim_succeeds_even_at_low_monotonic_time():
    # time.monotonic() counts from an arbitrary epoch — often boot. On a freshly
    # booted runner it is smaller than the throttle window, and a 0.0-default
    # claim map read as "claimed at monotonic zero", refusing every first claim.
    from app import auth

    pat_id = 999_901
    auth._pat_last_used_claimed.pop(pat_id, None)
    assert auth.claim_pat_stamp(pat_id, 5.0) is True
    assert auth.claim_pat_stamp(pat_id, 6.0) is False


def test_a_failing_stamp_never_breaks_pat_authentication(pat_client, db_engine, monkeypatch):
    # The stamp is bookkeeping in a task of its own; a canceled UPDATE on the locked
    # PAT row (or any transient DB error inside it) fails that task alone and must not
    # turn a valid token into a 500 or expire loaded state.
    pat_id = _seed()
    try:

        async def _boom(pat_id: int, now: datetime) -> None:
            raise RuntimeError("stamp exploded")

        monkeypatch.setattr(auth, "stamp_pat_last_used", _boom)
        r = pat_client.get("/api/v1/memos", headers=HEADERS)
        assert r.status_code == 200
        _drain(pat_client)  # the drain must not re-raise the task's failure either
        assert _last_used(pat_id) is None
    finally:
        _cleanup()


async def test_a_failing_stamp_task_is_logged_not_silently_dropped(monkeypatch):
    """Nothing awaits a scheduled stamp, so without the done callback its exception would vanish
    (or resurface as an 'exception was never retrieved' warning at GC time, detached from the
    request that caused it)."""

    async def _boom(pat_id: int, now: datetime) -> None:
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(auth, "stamp_pat_last_used", _boom)
    with capture_logs() as logs:
        task = auth.schedule_pat_last_used_stamp(999_902, None)
        assert task is not None
        # asyncio.wait, not `await task`: awaiting would re-raise into this caller and prove
        # nothing about a failure that nobody is waiting for. The done callback registered at
        # schedule time runs first (callbacks fire in registration order).
        await asyncio.wait([task])
    assert any(entry["event"] == "pat_last_used_stamp_failed" for entry in logs)
    assert task not in auth._stamp_tasks
    assert task.exception() is not None  # retrieved by the callback, so no loop-level warning


async def test_scheduled_stamp_is_strongly_referenced_until_it_finishes(db_engine):
    """The event loop keeps only a WEAK reference to a task, so a fire-and-forget stamp that no
    one holds can be garbage-collected mid-write; `auth._stamp_tasks` is that owner. The GC race
    itself is not deterministically reproducible from a test (this frame holds its own reference,
    and the loop's ready-queue handle holds one until the first step), so what is pinned here is
    the registry: the task is in it while in flight and out of it once done."""
    pat_id = _seed()
    try:
        task = auth.schedule_pat_last_used_stamp(pat_id, None)
        assert task is not None
        assert task in auth._stamp_tasks
        await task
        assert _last_used(pat_id) is not None
        assert task not in auth._stamp_tasks  # the done callback releases it (no unbounded growth)
    finally:
        _cleanup()


async def test_drain_awaits_an_in_flight_stamp(db_engine):
    """What the lifespan shutdown hook promises: a stamp in flight when the app stops still lands
    (this is also the mechanism every read-back test above depends on)."""
    pat_id = _seed()
    try:
        assert auth.schedule_pat_last_used_stamp(pat_id, None) is not None
        await auth.drain_pat_stamp_tasks()
        assert _last_used(pat_id) is not None
        assert not auth._stamp_tasks
    finally:
        _cleanup()


async def test_drain_cancels_a_stamp_that_outlives_its_timeout(monkeypatch):
    """A stamp must never hold shutdown open: past the bound it is cancelled deliberately, rather
    than being destroyed mid-write by the loop teardown with the engine already going away."""
    started = asyncio.Event()

    async def _hang(pat_id: int, now: datetime) -> None:
        started.set()
        await asyncio.Event().wait()  # never completes

    monkeypatch.setattr(auth, "stamp_pat_last_used", _hang)
    task = auth.schedule_pat_last_used_stamp(999_903, None)
    assert task is not None
    await started.wait()  # the task's own signal — deterministic, not a sleep
    await auth.drain_pat_stamp_tasks(grace_s=0)
    assert task.cancelled()
    assert not auth._stamp_tasks


# --- AC1 pinned structurally: the request path never awaits the second-connection write ---

APP_DIR = Path(auth.__file__).resolve().parent


def _await_call_names(node: ast.AST) -> set[str]:
    """Names of the functions called in `await <call>` expressions anywhere under `node`."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Await) and isinstance(call := child.value, ast.Call):
            func = call.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"async def {name} not found")


def test_no_async_function_in_app_awaits_the_stamp_write_inline():
    """AC1. `stamp_pat_last_used` opens a SECOND session; awaiting it from a handler that still
    holds the request's connection is exactly the hold-and-wait the ticket removes. It may only be
    reached by being handed to create_task — a revert to `await stamp_pat_last_used(...)` anywhere
    under app/ turns this red."""
    offenders = []
    scheduled_by_create_task = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        if "stamp_pat_last_used" in _await_call_names(tree):
            offenders.append(str(path.relative_to(APP_DIR.parent)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_task"
                and any(
                    isinstance(a, ast.Call)
                    and isinstance(a.func, ast.Name)
                    and a.func.id == "stamp_pat_last_used"
                    for a in node.args
                )
            ):
                scheduled_by_create_task += 1
    assert offenders == []
    assert scheduled_by_create_task == 1  # reached only through the scheduler


def test_authenticate_pat_schedules_the_stamp_without_awaiting_it():
    """AC1, from the other side: the request path still triggers the stamp — it just doesn't wait
    for it. (Deleting the call would leave the test above trivially green.)"""
    tree = ast.parse(Path(auth.__file__).read_text())
    fn = _function(tree, "authenticate_pat")
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "schedule_pat_last_used_stamp"
    ]
    assert len(calls) == 1
    assert "schedule_pat_last_used_stamp" not in _await_call_names(fn)
    assert _await_call_names(fn) <= {"execute"}  # only the request session's own queries remain
