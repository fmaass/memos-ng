"""Home noticing that its trip cursor is ahead of the door (MEMNG-1205).

Every test here runs a whole tick through `run_sync_safe`, through the real `make_client`, against
a stub door on a real loopback socket: what home reads is what the wire carried, and what it did is
read back from its own tables. The real door's side of the contract (where `head` comes from) is
held in tests/test_ingest_service.py.
"""

import hashlib
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
import structlog
from app.config import settings
from app.database import async_session
from app.models import User
from app.services import collections as coll_svc
from app.services import passes as passes_svc
from app.services import trip_ingest, trip_ingest_metrics

from tests.ingest_support import JPEG
from tests.test_attachments_taken import _sql
from tests.test_trip_ingest_puller import HOME_TABLES

INT64_MAX = 2**63 - 1
OWNER = "cursor_ahead_owner"
DEVICES = "/ingest/devices"
BATCHES = "/ingest/batches"
ROUTE = {"devices": DEVICES, "batches": BATCHES}


def _device(uid: str, pass_uid: str) -> dict:
    return {
        "device_id": uid,
        "pass_uid": pass_uid,
        "display_name": "Anna",
        "platform": "web",
        "first_seen": "2031-03-01T10:00:00+00:00",
        "last_seen": "2031-03-01T11:00:00+00:00",
        "uploads_count": 1,
        "bytes_total": 100,
        "revoked_at": None,
        "revoked_version": None,
    }


class StubDoor:
    """The home interface on a real socket. Honest by default: `seq` is its sequence, and its pages
    report that as `head` unless `reports_head` is off, the door of the release before this check.
    `answers` replaces one feed route's page with whatever a test wants on the wire."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.seq = 0
        self.reports_head = True
        self.devices: list[dict] = []
        self.batches: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.answers: dict[str, Callable[[int], dict]] = {}
        self.calls: list[tuple[str, str, bytes]] = []
        door = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _serve(self, method: str) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                door.calls.append((method, url.path, body))
                if self.headers.get("authorization") != f"Bearer {door.token}":
                    self._send(401, b'{"detail":"no"}')
                    return
                self._send(*door.respond(method, url.path, parse_qs(url.query), body))

            def _send(self, status: int, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._serve("GET")

            def do_PUT(self) -> None:  # noqa: N802
                self._serve("PUT")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_device(self, device: dict) -> int:
        self.seq += 1
        self.devices.append({**device, "cursor": self.seq})
        return self.seq

    def add_batch(self, batch: dict) -> int:
        self.seq += 1
        self.batches.append({**batch, "cursor": self.seq})
        return self.seq

    def restore(self, seq: int) -> None:
        """The door comes back from a backup taken when its sequence stood at `seq`."""
        self.seq = seq
        self.devices = [d for d in self.devices if d["cursor"] <= seq]
        self.batches = [b for b in self.batches if b["cursor"] <= seq]

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, path, _ in self.calls if m == method and path.endswith(suffix))

    def _page(self, route: str, key: str, items: list[dict], query: dict) -> bytes:
        since = int(query.get("since", ["0"])[0])
        if route in self.answers:
            return json.dumps(self.answers[route](since)).encode()
        page = [i for i in items if i["cursor"] > since]
        out: dict = {key: page, "next": page[-1]["cursor"] if page else since}
        if self.reports_head:
            out["head"] = self.seq
        return json.dumps(out).encode()

    def respond(self, method: str, path: str, query: dict, body: bytes) -> tuple[int, bytes]:
        if method == "PUT" and path == "/ingest/passes":
            passes = json.loads(body)["passes"]
            answer = [
                {"uid": p["uid"], "version": p["version"], "applied": True, "suspended_at": None}
                for p in passes
            ]
            return 200, json.dumps(answer).encode()
        if method == "PUT" and path == "/ingest/device-revocations":
            applied = json.loads(body)["revocations"]
            return 200, json.dumps(
                {"applied": applied, "unknown": [], "highest_version": None}
            ).encode()
        if method == "GET" and path == DEVICES:
            return 200, self._page(DEVICES, "devices", self.devices, query)
        if method == "GET" and path == BATCHES:
            return 200, self._page(BATCHES, "batches", self.batches, query)
        if method == "GET" and path.startswith("/ingest/blobs/"):
            data = self.blobs.get(path.rsplit("/", 1)[-1])
            return (200, data) if data is not None else (404, b'{"detail":"Not found"}')
        if method == "POST" and path.startswith("/ingest/batches/"):
            uid, verb = path.split("/")[3:5]
            return 200, json.dumps({"batch_id": uid, "state": verb}).encode()
        if method == "GET" and path == "/ingest/passes/suspended":
            return 200, b'{"passes":[]}'
        return 404, b'{"detail":"Not found"}'


@dataclass
class Home:
    door: StubDoor
    pass_uid: str
    owner_id: int
    device_id: str = "devA1234"

    def batch(self, uid: str, device_id: str | None = None, **overrides) -> dict:
        """A batch home would file, its bytes on the door. Not on the feed yet."""
        data = JPEG + uid.encode()
        digest = hashlib.sha256(data).hexdigest()
        self.door.blobs[digest] = data
        return {
            "batch_id": uid,
            "pass_uid": self.pass_uid,
            "device_id": device_id or self.device_id,
            "display_name": "Anna",
            "platform": "web",
            "caption": None,
            "state": "complete",
            "completed_at": "2031-03-02T10:00:00+00:00",
            "blobs": [
                {
                    "sha256": digest,
                    "size": len(data),
                    "mime": "image/jpeg",
                    "filename": "a.jpg",
                    "last_modified": "2031-03-02T09:00:00+00:00",
                }
            ],
            **overrides,
        }


def _wipe() -> None:
    for table in HOME_TABLES:
        _sql(f"DELETE FROM {table}")
    _sql("DELETE FROM users WHERE subject = :s", {"s": OWNER})


@pytest_asyncio.fixture
async def home(db_engine, tmp_path, monkeypatch) -> AsyncIterator[Home]:
    token_file = tmp_path / "door-token"
    token_file.write_text("stub-door-token\n")
    door = StubDoor("stub-door-token")
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path / "home"))
    monkeypatch.setattr(settings, "TRIP_INGEST_URL", door.url)
    monkeypatch.setattr(settings, "TRIP_INGEST_TOKEN_FILE", str(token_file))
    _wipe()
    async with async_session() as db:
        owner = User(subject=OWNER, display_name="Owner")
        db.add(owner)
        await db.commit()
        owner_id = owner.id
    async with async_session() as db:
        owner = await db.get(User, owner_id)
        assert owner is not None
        trip, _ = await coll_svc.create_collection(
            db, owner, title="Trip", kind="trip", tag="cursorahead"
        )
        issued, _secret = await passes_svc.issue_pass(db, trip, owner, label="friends")
        await db.commit()
        pass_uid = issued.uid
    world = Home(door, pass_uid, owner_id)
    door.add_device(_device(world.device_id, pass_uid))
    try:
        yield world
    finally:
        door.close()
        _wipe()


async def _tick() -> str:
    return await trip_ingest.run_sync_safe(budget_s=30)


@dataclass(frozen=True)
class Cursor:
    devices_since: int
    batches_since: int
    last_sync_at: datetime | None
    last_error: str | None


def _cursor() -> Cursor:
    return Cursor(
        *_sql(
            "SELECT devices_since, batches_since, last_sync_at, last_error "
            "FROM trip_ingest_cursor WHERE id = 1"
        )[0]
    )


def _lower_cursors(to: int) -> None:
    """What the runbook's owner step does: nothing in home does this on its own."""
    _sql("UPDATE trip_ingest_cursor SET devices_since = :v, batches_since = :v", {"v": to})


def _ledger(uid: str) -> list[tuple]:
    return _sql(
        "SELECT state, memo_id IS NOT NULL, quarantined_at IS NOT NULL "
        "FROM ingest_batches WHERE uid = :u",
        {"u": uid},
    )


FILED = [("acked", True, False)]


def _memos() -> int:
    return _sql("SELECT count(*) FROM memos")[0][0]


def _named(error: str | None, feed: str, *numbers: int) -> bool:
    assert error is not None
    assert error.startswith("cursor_ahead:"), error
    return feed in error and all(str(n) in error for n in numbers)


# --- a jump: the reproducers from the red-team day ------------------------------------------------


async def test_a_batch_jump_commits_no_cursor_files_nothing_and_a_later_batch_still_lands(home):
    """`case7.py cursor next_jumps`: the honest head comes with a `next` that lies past it."""
    assert await _tick() == "ok"
    before = _cursor()
    batch = home.batch("jumpBatch1")
    head = home.door.add_batch(batch)
    sent = home.door.batches[-1]
    home.door.answers[BATCHES] = lambda since: {"batches": [sent], "next": INT64_MAX, "head": head}

    with structlog.testing.capture_logs() as logs:
        assert await _tick() == "cursor_ahead"

    after = _cursor()
    assert after.batches_since == before.batches_since  # the harm was the committed cursor
    assert after.last_sync_at == before.last_sync_at  # must not read healthy
    assert after.last_error == (
        f"cursor_ahead: batches since={before.batches_since} next={INT64_MAX} head={head}"
    )
    assert _ledger("jumpBatch1") == []
    assert _memos() == 0
    logged = [e for e in logs if e["event"] == "trip_ingest.cursor_ahead"]
    assert logged and logged[0]["log_level"] == "error"
    assert _named(logged[0]["error"], "batches", INT64_MAX, head)

    # The door is honest again: the page that was refused is simply served again.
    home.door.answers.clear()
    assert await _tick() == "ok"
    assert _ledger("jumpBatch1") == FILED
    assert _memos() == 1
    assert _cursor().batches_since == head
    assert _cursor().last_error is None


async def test_a_device_jump_commits_no_cursor_and_quarantines_no_honest_batch(home):
    """`bomb.py device_jump`: before the check, a new guest's device was never pulled, so its
    honest batch was quarantined at home and on the door."""
    assert await _tick() == "ok"
    before = _cursor()
    home.door.answers[DEVICES] = lambda since: {
        "devices": [_device("devJump01", home.pass_uid)],
        "next": INT64_MAX,
        "head": home.door.seq,
    }

    assert await _tick() == "cursor_ahead"

    after = _cursor()
    assert after.devices_since == before.devices_since
    assert after.last_sync_at == before.last_sync_at
    assert _named(after.last_error, "devices", INT64_MAX, home.door.seq)
    assert _sql("SELECT uid FROM pass_devices WHERE uid = 'devJump01'") == []

    home.door.answers.clear()
    home.door.add_device(_device("newGuest1", home.pass_uid))
    home.door.add_batch(home.batch("honest01", "newGuest1"))
    assert await _tick() == "ok"
    assert _ledger("honest01") == FILED
    assert home.door.count("POST", "/honest01/quarantine") == 0
    assert _memos() == 1


async def test_a_jump_in_the_drain_a_batch_page_starts_refuses_that_page_too(home):
    """A batch naming an unknown device drains the device feed again mid-walk. A jump seen there
    must not fall through to judging the page, which would quarantine the honest batch."""
    assert await _tick() == "ok"
    home.door.add_batch(home.batch("drained01", "newGuest2"))
    served: list[int] = []

    def devices(since: int) -> dict:
        served.append(since)
        if len(served) == 1:
            return {"devices": [], "next": since, "head": home.door.seq}
        return {
            "devices": [_device("newGuest2", home.pass_uid)],
            "next": INT64_MAX,
            "head": home.door.seq,
        }

    home.door.answers[DEVICES] = devices

    assert await _tick() == "cursor_ahead"

    assert len(served) == 2
    assert _named(_cursor().last_error, "devices", INT64_MAX, home.door.seq)
    assert _ledger("drained01") == []
    assert home.door.count("POST", "/drained01/quarantine") == 0


# --- a restored door: the honest silence ----------------------------------------------------------


@pytest.mark.parametrize(
    ("feed", "other_first"),
    [("devices", True), ("batches", False)],
    ids=["device_cursor_ahead", "batch_cursor_ahead"],
)
async def test_a_restored_door_with_empty_pages_reads_unhealthy(home, feed, other_first):
    """Home's cursor is above the restored door's head and the pages come back empty. Today's
    `if not batches: return seen` would end the walk with nothing wrong anywhere."""
    if other_first:
        restored_to = home.door.add_batch(home.batch("older0001"))
        home.door.add_device(_device("laterGuest", home.pass_uid))
    else:
        restored_to = home.door.seq
        home.door.add_batch(home.batch("later0001"))
    assert await _tick() == "ok"
    before = _cursor()
    ahead = before.devices_since if feed == "devices" else before.batches_since
    assert ahead > restored_to

    home.door.restore(restored_to)
    assert await _tick() == "cursor_ahead"

    after = _cursor()
    assert (after.devices_since, after.batches_since) == (
        before.devices_since,
        before.batches_since,
    )
    assert after.last_sync_at == before.last_sync_at
    assert after.last_error == f"cursor_ahead: {feed} since={ahead} next={ahead} head={restored_to}"


async def test_the_refusal_keeps_the_stale_alert_armed_on_the_gauges_it_reads(home):
    """`GuestSyncStale` is `guest_sync_snapshot_age_seconds > 900 and guest_sync_required == 1`.
    The whole tick really runs: the age holds only because the refusal does not stamp the sync."""
    assert await _tick() == "ok"
    old = datetime.now(UTC) - timedelta(hours=2)
    _sql("UPDATE trip_ingest_cursor SET last_sync_at = :t", {"t": old})
    home.door.restore(0)
    assert _cursor().devices_since > home.door.seq

    assert await _tick() == "cursor_ahead"

    state = await trip_ingest_metrics.read_state()
    now = datetime.now(UTC)
    gauges = {f.name: f.samples[0].value for f in trip_ingest_metrics.families_from(state, now)}
    assert gauges[trip_ingest_metrics.SNAPSHOT_AGE] > 900
    assert gauges[trip_ingest_metrics.REQUIRED] == 1.0


async def test_a_refused_tick_still_pushes_pass_state_and_files_no_open_batch(home):
    """Revocations must keep reaching the door, and they run first. Nothing from the feed is filed,
    not even a batch an earlier tick pulled and left open."""
    assert await _tick() == "ok"
    home.door.add_batch(home.batch("openBatch1"))
    async with trip_ingest.make_client(home.door.token) as client:
        await trip_ingest.pull_batches(client, [])
    assert _ledger("openBatch1") == [("open", False, False)]

    async with async_session() as db:
        owner = await db.get(User, home.owner_id)
        assert owner is not None
        trip, _ = await coll_svc.create_collection(
            db, owner, title="Second", kind="trip", tag="cursorahead2"
        )
        second, _secret = await passes_svc.issue_pass(db, trip, owner, label="second")
        await db.commit()
        second_uid = second.uid

    home.door.restore(0)
    home.door.calls.clear()
    assert await _tick() == "cursor_ahead"

    pushed = [body for m, path, body in home.door.calls if (m, path) == ("PUT", "/ingest/passes")]
    assert any(second_uid in body.decode() for body in pushed)
    assert _ledger("openBatch1") == [("open", False, False)]
    assert _memos() == 0


# --- recovery: only the operator lowers a cursor --------------------------------------------------


async def test_no_cursor_moves_until_the_operator_lowers_it_and_then_the_door_drains(home):
    head = home.door.add_batch(home.batch("restored01"))
    assert await _tick() == "ok"
    assert _ledger("restored01") == FILED
    home.door.restore(1)
    before = _cursor()
    for _ in range(3):  # no automatic reset, however often the tick runs
        assert await _tick() == "cursor_ahead"
    refused = _cursor()
    assert (refused.devices_since, refused.batches_since, refused.last_sync_at) == (
        before.devices_since,
        before.batches_since,
        before.last_sync_at,
    )
    assert head > 1

    _lower_cursors(1)
    home.door.add_batch(home.batch("afterwards"))
    assert await _tick() == "ok"

    after = _cursor()
    assert after.last_error is None
    assert after.last_sync_at is not None and before.last_sync_at is not None
    assert after.last_sync_at > before.last_sync_at
    assert after.batches_since == home.door.seq
    assert _ledger("afterwards") == FILED


# --- a door without the field: the pluto-first deploy ---------------------------------------------


async def test_a_door_without_head_files_as_it_always_did(home):
    home.door.reports_head = False
    head = home.door.add_batch(home.batch("oldDoor001"))
    assert await _tick() == "ok"
    assert _ledger("oldDoor001") == FILED
    after = _cursor()
    assert after.batches_since == head
    assert after.last_error is None


async def test_a_door_without_head_keeps_todays_answer_to_a_jump_and_to_a_restore(home):
    """Pinned as it behaves today, on purpose: the check needs the field, and the old door lacks it.
    The two cases this ticket closes for a door that reports `head`."""
    home.door.reports_head = False
    assert await _tick() == "ok"
    home.door.restore(0)
    assert _cursor().devices_since > home.door.seq
    assert await _tick() == "ok"  # above the door's sequence, empty pages, silence
    assert _cursor().last_error is None

    home.door.answers[BATCHES] = lambda since: {"batches": [], "next": since}
    batch = home.batch("oldJump001")
    home.door.add_batch(batch)
    sent = home.door.batches[-1]
    home.door.answers[BATCHES] = lambda since: {"batches": [sent], "next": INT64_MAX}
    assert await _tick() == "ok"
    assert _cursor().batches_since == INT64_MAX
    assert _cursor().last_error is None


# --- a malformed head: `LinkDown`, like any other malformed page ----------------------------------

MALFORMED = ["3", -1, INT64_MAX + 1, 1.5, True, None, [3], {"v": 3}]
MALFORMED_IDS = ["string", "negative", "past_bigint", "float", "bool", "null", "list", "object"]


# Every value on an empty page (the check must run before the walk ends) and one on a full page, for
# each feed. The whole cross takes a minute of the gate and adds no branch.
MALFORMED_CASES = [
    pytest.param(feed, True, head, id=f"{feed}-empty-{name}")
    for feed in ("devices", "batches")
    for head, name in zip(MALFORMED, MALFORMED_IDS, strict=True)
] + [pytest.param(feed, False, "3", id=f"{feed}-full-string") for feed in ("devices", "batches")]


@pytest.mark.parametrize(("feed", "empty", "head"), MALFORMED_CASES)
async def test_a_malformed_head_is_a_malformed_page_and_moves_nothing(home, feed, empty, head):
    assert await _tick() == "ok"
    before = _cursor()
    home.door.add_batch(home.batch("malformed1"))
    if feed == "devices":
        home.door.add_device(_device("malformDev", home.pass_uid))
    entry = (home.door.devices if feed == "devices" else home.door.batches)[-1]

    def page(since: int) -> dict:
        items = [] if empty else [entry]
        return {feed: items, "next": since if empty else entry["cursor"], "head": head}

    home.door.answers[ROUTE[feed]] = page

    assert await _tick() == "link_down"

    after = _cursor()
    assert after.last_error == f"link: GET /ingest/{feed}: malformed page"
    assert (after.devices_since, after.batches_since) == (
        before.devices_since,
        before.batches_since,
    )
    assert after.last_sync_at == before.last_sync_at
    assert _ledger("malformed1") == []
    assert _memos() == 0
    assert _sql("SELECT uid FROM pass_devices WHERE uid = 'malformDev'") == []


# --- boundaries -----------------------------------------------------------------------------------


async def test_a_door_that_never_drew_a_cursor_answers_head_zero_and_is_healthy(home):
    home.door.devices.clear()
    home.door.seq = 0
    assert await _tick() == "ok"
    after = _cursor()
    assert (after.devices_since, after.batches_since, after.last_error) == (0, 0, None)


@pytest.mark.parametrize("feed", ["devices", "batches"])
async def test_next_exactly_at_the_head_passes_and_one_above_is_refused(home, feed):
    assert await _tick() == "ok"
    home.door.add_batch(home.batch("edge000001"))
    home.door.add_device(_device("edgeDevice", home.pass_uid))
    key = "devices" if feed == "devices" else "batches"
    entry = (home.door.devices if feed == "devices" else home.door.batches)[-1]
    nxt = entry["cursor"]
    since_before = getattr(_cursor(), f"{feed}_since")

    home.door.answers[ROUTE[feed]] = lambda since: (
        {key: [entry], "next": nxt, "head": nxt - 1}
        if since == since_before
        else {key: [], "next": since, "head": max(since, nxt)}
    )
    assert await _tick() == "cursor_ahead"
    assert getattr(_cursor(), f"{feed}_since") == since_before
    assert _named(_cursor().last_error, feed, nxt, nxt - 1)

    home.door.answers[ROUTE[feed]] = lambda since: (
        {key: [entry], "next": nxt, "head": nxt}
        if since == since_before
        else {key: [], "next": since, "head": max(since, nxt)}
    )
    assert await _tick() == "ok"
    assert getattr(_cursor(), f"{feed}_since") == nxt
    assert _cursor().last_error is None


@pytest.mark.parametrize("feed", ["devices", "batches"])
async def test_a_cursor_exactly_at_the_head_is_healthy_and_one_above_is_refused(home, feed):
    head = home.door.add_batch(home.batch("edge000002"))
    assert await _tick() == "ok"
    _lower_cursors(head)
    home.door.answers[ROUTE[feed]] = lambda since: {feed: [], "next": since, "head": since}
    assert await _tick() == "ok"
    assert _cursor().last_error is None

    home.door.answers[ROUTE[feed]] = lambda since: {feed: [], "next": since, "head": since - 1}
    assert await _tick() == "cursor_ahead"
    assert _named(_cursor().last_error, feed, head, head - 1)


# --- the field is the page's own key, nothing that merely says `head` -----------------------------


async def test_text_that_says_head_is_not_the_field(home):
    """The door is honest and reports a real head above these: a caption, a file name and an entry
    key spelling a lower one must not be read as the page's field."""
    assert await _tick() == "ok"
    home.door.add_batch(
        home.batch("textHead01", caption='"head": 0', head=0, display_name='{"head": 0}')
    )
    assert await _tick() == "ok"
    assert _ledger("textHead01") == FILED
    assert _cursor().last_error is None
