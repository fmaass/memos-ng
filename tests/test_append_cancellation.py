"""A cancelled append keeps its row lock until its writer thread has returned (MEMNG-1028).

The chunk append writes the part file in a worker thread while the request holds the session's row
lock. A worker thread cannot be cancelled, so a request cancelled mid-append must not let its
transaction — and with it the lock — unwind before the thread is done: a retry would take the lock,
find the same committed offset, and write at it while the first writer still runs.

Both callers of the shared seam are driven through their real routes: the member PATCH and the
guest door's PATCH. The write is parked on a `threading.Event` INSIDE the patched `_concat_sync`,
so the thread is really running and really blocked; the retry is proved to be waiting on the row
lock by Postgres's own `pg_stat_activity` before the first request is cancelled, so the race the
cancellation opens is the only thing the test measures.
"""

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from app.config import settings
from app.services import uploads as uploads_svc

from tests.ingest_support import (  # noqa: F401
    bearer,
    door,
    home_iface,
    ingest_env,
    redeem,
    seeded_pass,
)
from tests.test_attachments_taken import _sql
from tests.test_capture_concurrency import _async_client, _cleanup
from tests.test_lock_protocol_concurrency import _lock_waiters

SEAM_TIMEOUT = 30.0
POLL = 0.02
# How long a retry is given to break in once the first request is cancelled. Unfixed, it writes
# within milliseconds — the unwind is one rollback; fixed, it never does, so the window only
# bounds the green run's length.
BREAK_IN_WINDOW = 2.0
SIZE = 4096


class ParkedConcat:
    """`_concat_sync` with its FIRST call parked on a threading event before it writes.

    The log is the order writers entered and returned in, taken under a thread lock, so it is the
    threads' own account and not the event loop's.
    """

    def __init__(self) -> None:
        self.real = uploads_svc._concat_sync
        self.lock = threading.Lock()
        self.log: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def calls(self) -> int:
        with self.lock:
            return sum(1 for e in self.log if e.startswith("enter"))

    def __call__(self, spool: Path, part: Path, offset: int) -> None:
        with self.lock:
            n = 1 + sum(1 for e in self.log if e.startswith("enter"))
            self.log.append(f"enter{n}")
        try:
            if n == 1:
                self.entered.set()
                assert self.release.wait(SEAM_TIMEOUT), "the parked writer was never released"
            self.real(spool, part, offset)
        finally:
            with self.lock:
                self.log.append(f"return{n}")


async def _until(predicate: Callable[[], bool], what: str, ceiling: float = SEAM_TIMEOUT) -> None:
    deadline = time.monotonic() + ceiling
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(what)
        await asyncio.sleep(POLL)


async def _new_lock_waiter(before: set[int]) -> int:
    deadline = time.monotonic() + SEAM_TIMEOUT
    while time.monotonic() < deadline:
        fresh = [pid for pid in await asyncio.to_thread(_lock_waiters) if pid not in before]
        if fresh:
            return fresh[0]
        await asyncio.sleep(POLL)
    raise AssertionError("the retry never blocked on the row lock — this test proves nothing")


async def _cancel_mid_append_then_retry(
    patch: Callable[[bytes], Awaitable[httpx.Response]], part: Path, monkeypatch, cancels: int
) -> None:
    first_body = b"\xff\xd8\xff" + b"a" * (SIZE - 3)
    retry_body = b"\xff\xd8\xff" + b"b" * (SIZE - 3)
    parked = ParkedConcat()
    monkeypatch.setattr(uploads_svc, "_concat_sync", parked)
    first = asyncio.create_task(patch(first_body))
    retry: asyncio.Task[httpx.Response] | None = None
    try:
        await _until(parked.entered.is_set, "the first append never reached its write")
        before = set(await asyncio.to_thread(_lock_waiters))
        retry = asyncio.create_task(patch(retry_body))
        waiter = await _new_lock_waiter(before)

        # More than one cancellation is the shape a level-triggered cancel scope delivers: each
        # re-await of the writer must survive the next one too, not only the first.
        for _ in range(cancels):
            first.cancel()
            await asyncio.sleep(POLL)
        with contextlib.suppress(AssertionError):
            await _until(
                lambda: parked.calls > 1 or first.done(),
                "nothing broke in",
                ceiling=BREAK_IN_WINDOW,
            )
        assert parked.calls == 1, (
            f"the retry wrote while the cancelled request's writer was still running: {parked.log}"
        )
        assert not first.done(), (
            "the cancelled request unwound — and released the row lock — while its writer "
            "thread was still running"
        )
        assert waiter in await asyncio.to_thread(_lock_waiters), (
            "the retry is no longer waiting on the row lock, yet has not written"
        )

        parked.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=SEAM_TIMEOUT)
        assert first.cancelled(), "the append swallowed a cancellation its caller has to see"
        answered = await asyncio.wait_for(retry, timeout=SEAM_TIMEOUT)
        assert answered.status_code == 200, answered.text
        assert answered.headers["Upload-Offset"] == str(SIZE)
        assert parked.log == ["enter1", "return1", "enter2", "return2"]
        assert part.read_bytes() == retry_body
    finally:
        parked.release.set()
        for task in (first, retry):
            if task is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=SEAM_TIMEOUT)


@pytest.mark.parametrize("cancels", [1, 3])
async def test_a_cancelled_member_append_holds_the_lock_until_its_writer_returns(
    logged_in_client, monkeypatch, tmp_path, cancels
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        created = client.post(
            "/api/uploads",
            json={"filename": "a.jpg", "mime": "image/jpeg", "size": SIZE},
        )
        assert created.status_code == 201, created.text
        uid = created.json()["upload_uid"]
        async with _async_client(client) as ac:

            def patch(body: bytes) -> Awaitable[httpx.Response]:
                return ac.patch(f"/api/uploads/{uid}", content=body, headers={"Upload-Offset": "0"})

            await _cancel_mid_append_then_retry(
                patch, uploads_svc.part_path(uid), monkeypatch, cancels
            )
        assert _sql("SELECT bytes_received FROM uploads WHERE upload_uid=:u", {"u": uid}) == [
            (SIZE,)
        ]
    finally:
        _cleanup()


@pytest.mark.parametrize("cancels", [1, 3])
async def test_a_cancelled_guest_append_holds_the_lock_until_its_writer_returns(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    ingest_env,  # noqa: F811
    monkeypatch,
    cancels,
):
    _, secret = await seeded_pass(home_iface)
    token = (await redeem(door, secret))["device_token"]
    created = await door.post(
        "/api/guest/uploads",
        json={"filename": "a.jpg", "mime": "image/jpeg", "size": SIZE},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    uid = created.json()["upload_uid"]

    def patch(body: bytes) -> Awaitable[httpx.Response]:
        return door.patch(
            f"/api/guest/uploads/{uid}",
            content=body,
            headers={**bearer(token), "Upload-Offset": "0"},
        )

    await _cancel_mid_append_then_retry(
        patch, ingest_env / "parts" / f"{uid}.part", monkeypatch, cancels
    )
    assert _sql("SELECT bytes_received FROM guest_uploads WHERE upload_uid=:u", {"u": uid}) == [
        (SIZE,)
    ]
