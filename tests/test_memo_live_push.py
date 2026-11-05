"""MEMNG-967: one 'memo is live' push per memo, to its owner and the opted-in household.

Every push here is captured at the web-push SENDER boundary (`pywebpush.webpush`, the one call that
would leave the process), never by replacing the code under test: the worker drains real media jobs
through `run_once`/`run_media_drain`, and the create route is driven over HTTP.
"""

import asyncio
import json
import secrets
import threading
from collections import Counter
from pathlib import Path

import pytest
import pywebpush
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app import worker
from app.config import settings
from app.database import async_session
from app.models import JobKind
from app.services import memo_live, notify, reclaim
from PIL import Image
from sqlalchemy import create_engine, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

MEMO_LIVE_REVISION = "c4e1a7d93b52"


def _sql(stmt: str, params: dict | None = None):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        result = c.execute(text(stmt), params or {})
        rows = result.fetchall() if result.returns_rows else []
    eng.dispose()
    return rows


class _Pushes:
    """Everything that reached the web-push sender, as (endpoint, payload)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._lock = threading.Lock()  # the sender runs in worker threads

        # memo uid -> files still in flight when its memo-live push left, read at the boundary
        self.in_flight_at_send: dict[str, list[int]] = {}

    def __call__(self, **kw) -> None:
        payload = json.loads(kw["data"])
        if payload.get("kind") == "memo_live":
            [(n,)] = _sql(
                "SELECT count(*) FROM attachments a JOIN memos m ON a.memo_id = m.id "
                "WHERE m.uid = :u AND a.status IN ('uploading', 'pending', 'processing')",
                {"u": payload["uid"]},
            )
            with self._lock:
                self.in_flight_at_send.setdefault(payload["uid"], []).append(n)
        with self._lock:
            self.sent.append((kw["subscription_info"]["endpoint"], payload))

    def live(self, prefix: str) -> list[tuple[str, dict]]:
        return [
            (e, p) for e, p in self.sent if e.startswith(prefix) and p.get("kind") == "memo_live"
        ]

    def live_per_endpoint(self, prefix: str) -> Counter:
        return Counter(e for e, _ in self.live(prefix))

    def attachment_pushes(self, endpoint: str) -> list[dict]:
        return [p for e, p in self.sent if e == endpoint and "kind" not in p]


@pytest.fixture
def pushes(monkeypatch, db_engine, tmp_path) -> _Pushes:
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-vapid-key")
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    sink = _Pushes()
    monkeypatch.setattr(pywebpush, "webpush", sink)
    return sink


class _Household:
    """Three users, each with one push subscription: the owner, a member who opted in to household
    posts and one who did not. Every row is committed (the worker reads in its own sessions)."""

    def __init__(self) -> None:
        tag = secrets.token_hex(4)
        self.prefix = f"https://push.test/{tag}/"
        self.ids: dict[str, int] = {}
        for who, opt_in, name in (
            ("owner", False, "Olga Owner"),
            ("member_on", True, "Mia"),
            ("member_off", False, "Max"),
        ):
            uid = _sql(
                "INSERT INTO users (subject, display_name, role, default_visibility, theme, "
                "notify_on_household_posts) VALUES (:s, :n, 'user', 'PUBLIC', 'dark', :o) "
                "RETURNING id",
                {"s": f"ml_{who}_{tag}", "n": name, "o": opt_in},
            )[0][0]
            self.ids[who] = uid
            _sql(
                "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
                "VALUES (:u, :e, 'k', 'a')",
                {"u": uid, "e": self.endpoint(who)},
            )

    def endpoint(self, who: str) -> str:
        return f"{self.prefix}{who}"

    def cleanup(self) -> None:
        for uid in self.ids.values():
            _sql(
                "DELETE FROM media_jobs WHERE attachment_id IN "
                "(SELECT id FROM attachments WHERE owner_id = :u)",
                {"u": uid},
            )
            _sql("DELETE FROM attachments WHERE owner_id = :u", {"u": uid})
            _sql("DELETE FROM memos WHERE owner_id = :u", {"u": uid})
            _sql("DELETE FROM users WHERE id = :u", {"u": uid})


@pytest.fixture
def household(pushes):
    h = _Household()
    try:
        yield h
    finally:
        h.cleanup()


def _memo(owner_id: int, visibility: str, content: str = "Beach day\nmore text") -> tuple[int, str]:
    uid = f"ml{secrets.token_hex(6)}"
    mid = _sql(
        "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
        "VALUES (:u, :o, :c, CAST(:v AS visibility), false) RETURNING id",
        {"u": uid, "o": owner_id, "c": content, "v": visibility},
    )[0][0]
    return mid, uid


def _files(tmp_path: Path, owner_id: int, memo_id: int, n: int) -> list[int]:
    """`n` real PNGs attached to the memo in `processing`, each with its queued fast derive job —
    exactly what finalize leaves behind for an uploaded photo."""
    ids = []
    for i in range(n):
        src = tmp_path / f"src-{memo_id}-{i}.png"
        Image.new("RGB", (32, 24), (10 + i, 90, 40)).save(src, "PNG")
        aid = _sql(
            "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status, "
            "storage_path, position) VALUES (:u, :o, :m, :f, 'image/png', 1, "
            "CAST('processing' AS attachment_status), :sp, :p) RETURNING id",
            {
                "u": secrets.token_urlsafe(8),
                "o": owner_id,
                "m": memo_id,
                "f": f"p{i}.png",
                "sp": str(src),
                "p": i,
            },
        )[0][0]
        _sql(
            "INSERT INTO media_jobs (attachment_id, lane, kind, state, attempts, progress_pct) "
            "VALUES (:a, CAST('fast' AS job_lane), :k, CAST('queued' AS job_state), 0, 0)",
            {"a": aid, "k": JobKind.derive},
        )
        ids.append(aid)
    return ids


async def _drain(limit: int = 200) -> None:
    for _ in range(limit):
        if await worker.run_media_drain() == "idle":
            return
    raise AssertionError("the queue never drained")


def _statuses(memo_id: int) -> Counter:
    return Counter(
        r[0] for r in _sql("SELECT status FROM attachments WHERE memo_id = :m", {"m": memo_id})
    )


# --- acceptance ---


def test_a_thirty_file_memo_sends_exactly_one_live_push_per_recipient(pushes, household, tmp_path):
    mid, muid = _memo(household.ids["owner"], "PROTECTED", "  \n Beach day  \nsecond line")
    _files(tmp_path, household.ids["owner"], mid, 30)

    asyncio.run(_drain())

    assert _statuses(mid) == Counter({"ready": 30})  # the real terminal path ran for every file
    assert pushes.in_flight_at_send[muid] == [0, 0]  # sent only once the LAST file was done
    assert pushes.live_per_endpoint(household.prefix) == Counter(
        {household.endpoint("owner"): 1, household.endpoint("member_on"): 1}
    )  # the opted-out member got nothing
    [(_, payload)] = pushes.live(household.endpoint("owner"))
    assert payload == {
        "kind": "memo_live",
        "uid": muid,
        "owner": "Olga Owner",
        "title": "Beach day",
        "attachments": 30,
    }
    # The per-attachment push is unchanged: one 'ready' per file, to the owner only.
    owner_att = pushes.attachment_pushes(household.endpoint("owner"))
    assert len(owner_att) == 30 and {p["status"] for p in owner_att} == {"ready"}
    assert pushes.attachment_pushes(household.endpoint("member_on")) == []


def test_a_private_memo_pushes_its_owner_only(pushes, household, tmp_path):
    mid, _ = _memo(household.ids["owner"], "PRIVATE")
    _files(tmp_path, household.ids["owner"], mid, 2)

    asyncio.run(_drain())

    assert pushes.live_per_endpoint(household.prefix) == Counter({household.endpoint("owner"): 1})


@pytest.mark.parametrize("opt_in", [False, True])
def test_the_household_opt_in_decides_whether_a_member_hears_of_it(
    pushes, household, tmp_path, opt_in
):
    _sql(
        "UPDATE users SET notify_on_household_posts = :o WHERE id = :u",
        {"o": opt_in, "u": household.ids["member_off"]},
    )
    mid, _ = _memo(household.ids["owner"], "PUBLIC")
    _files(tmp_path, household.ids["owner"], mid, 1)

    asyncio.run(_drain())

    assert pushes.live_per_endpoint(household.prefix)[household.endpoint("member_off")] == (
        1 if opt_in else 0
    )


def test_two_concurrent_last_completions_send_once(pushes, household, tmp_path, monkeypatch):
    """Two consumers finish the memo's last two files at the same moment. Both commit `ready`
    first and are then held at the claim until BOTH have arrived, so each one, on its own, sees
    every file terminal — only the claim may decide which of them announces."""
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _files(tmp_path, household.ids["owner"], mid, 2)
    arrived = 0
    both = asyncio.Event()

    async def barrier() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=20)

    monkeypatch.setattr(memo_live, "_before_claim", barrier)

    async def two_consumers() -> list[str]:
        async def one() -> str:
            async with async_session() as db:
                return await worker.run_once(db)

        return list(await asyncio.gather(one(), one()))

    assert asyncio.run(two_consumers()) == ["processed", "processed"]
    assert arrived == 2  # the race really was run: both reached the claim
    assert _statuses(mid) == Counter({"ready": 2})
    assert pushes.live_per_endpoint(household.prefix) == Counter(
        {household.endpoint("owner"): 1, household.endpoint("member_on"): 1}
    )


# --- decisions the brief left open ---


def test_a_failed_file_is_terminal_and_the_memo_still_goes_live(
    pushes, household, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_MAX_ATTEMPTS", 1)
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    good, bad = _files(tmp_path, household.ids["owner"], mid, 2)
    _sql("UPDATE attachments SET storage_path = '/nonexistent/x.png' WHERE id = :a", {"a": bad})

    asyncio.run(_drain())

    assert _statuses(mid) == Counter({"ready": 1, "failed": 1})
    assert pushes.live_per_endpoint(household.prefix) == Counter(
        {household.endpoint("owner"): 1, household.endpoint("member_on"): 1}
    )
    [(_, payload)] = pushes.live(household.endpoint("member_on"))
    assert payload["attachments"] == 2


def test_a_memo_without_files_is_live_at_create(pushes, logged_in_client, db_engine):
    client, subject = logged_in_client
    h = _Household()
    try:
        r = client.post("/api/memos", json={"content": "Dinner at 7", "visibility": "PROTECTED"})
        assert r.status_code == 201
        uid = r.json()["uid"]
        live = pushes.live(h.prefix)
        assert [(e, p["uid"], p["title"], p["attachments"]) for e, p in live] == [
            (h.endpoint("member_on"), uid, "Dinner at 7", 0)
        ]
        # ...and once only: a later drain of an unrelated queue announces nothing more.
        asyncio.run(_drain())
        assert len(pushes.live(h.prefix)) == 1
    finally:
        _sql(
            "DELETE FROM memos WHERE owner_id = (SELECT id FROM users WHERE subject = :s)",
            {"s": subject},
        )
        h.cleanup()


def test_a_memo_whose_files_are_still_in_flight_is_not_live_at_create(pushes, household, tmp_path):
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _files(tmp_path, household.ids["owner"], mid, 1)

    async def at_create() -> bool:
        return await memo_live.announce_if_live(async_session, mid)

    assert asyncio.run(at_create()) is False
    assert pushes.live(household.prefix) == []


def test_a_guest_memo_names_its_contributor(pushes, household, tmp_path):
    mid, _ = _memo(household.ids["owner"], "PROTECTED", "#lisbon")
    [aid] = _files(tmp_path, household.ids["owner"], mid, 1)
    pass_id = _sql(
        "INSERT INTO passes (uid, created_by, secret_hash, label, expires_at) "
        "VALUES (:u, :o, :h, 'Lisbon', now() + interval '1 day') RETURNING id",
        {
            "u": f"pass{secrets.token_hex(4)}",
            "o": household.ids["owner"],
            "h": secrets.token_hex(16),
        },
    )
    _sql("UPDATE memos SET pass_id = :p WHERE id = :m", {"p": pass_id[0][0], "m": mid})
    _sql("UPDATE attachments SET contributor_name = 'Bob' WHERE id = :a", {"a": aid})
    try:
        asyncio.run(_drain())
        [(_, payload)] = pushes.live(household.endpoint("member_on"))
        assert payload["title"] == "Bob (guest)"
        assert payload["owner"] == "Olga Owner"
    finally:
        _sql("UPDATE memos SET pass_id = NULL WHERE id = :m", {"m": mid})
        _sql("DELETE FROM passes WHERE id = :p", {"p": pass_id[0][0]})


# --- pre-gate cases ---


def test_announcing_never_moves_the_memos_edit_time(pushes, household, tmp_path):
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _sql("UPDATE memos SET updated_at = '2020-01-01T00:00:00Z' WHERE id = :m", {"m": mid})
    _files(tmp_path, household.ids["owner"], mid, 1)

    asyncio.run(_drain())

    [(updated, live)] = _sql(
        "SELECT updated_at, live_notified_at FROM memos WHERE id = :m", {"m": mid}
    )
    assert live is not None
    assert updated.year == 2020  # the claim is not an edit


def test_a_failing_attachment_push_does_not_cost_the_memo_its_live_push(
    pushes, household, tmp_path, monkeypatch
):
    async def boom(db, attachment_id: int) -> None:
        raise RuntimeError("attachment push backend down")

    monkeypatch.setattr(worker, "_notify_terminal", boom)
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _files(tmp_path, household.ids["owner"], mid, 1)

    asyncio.run(_drain())

    assert pushes.live_per_endpoint(household.prefix)[household.endpoint("member_on")] == 1


def test_an_already_announced_memo_is_never_announced_again(pushes, household, tmp_path):
    """A re-derive of a file on a memo that went live long ago (or before this column existed —
    the migration marks every existing memo announced) is not a new memo."""
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _sql("UPDATE memos SET live_notified_at = now() WHERE id = :m", {"m": mid})
    _files(tmp_path, household.ids["owner"], mid, 1)

    asyncio.run(_drain())

    assert pushes.live(household.prefix) == []
    assert len(pushes.attachment_pushes(household.endpoint("owner"))) == 1  # unchanged


def test_the_migration_marks_existing_memos_announced_and_new_ones_not(logged_in_client):
    _, subject = logged_in_client
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    below = ScriptDirectory.from_config(cfg).get_revision(MEMO_LIVE_REVISION).down_revision
    assert isinstance(below, str)
    owner = _sql("SELECT id FROM users WHERE subject = :s", {"s": subject})[0][0]
    try:
        command.downgrade(cfg, below)
        old_id, _ = _memo(owner, "PROTECTED")
    finally:
        command.upgrade(cfg, "head")
    new_id, _ = _memo(owner, "PROTECTED")
    try:
        rows = dict(
            _sql(
                "SELECT id, live_notified_at IS NOT NULL FROM memos WHERE id IN (:a, :b)",
                {"a": old_id, "b": new_id},
            )
        )
        assert rows == {old_id: True, new_id: False}
        [(default,)] = _sql(
            "SELECT column_default FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='memos' AND column_name='live_notified_at'"
        )
        assert default is None
    finally:
        _sql("DELETE FROM memos WHERE id IN (:a, :b)", {"a": old_id, "b": new_id})


# --- MEMNG-1184: memos that go live without a worker completion, and visibility at send time ---


def test_a_memo_made_private_mid_delivery_reaches_no_further_household_member(
    pushes, household, tmp_path, monkeypatch
):
    """The owner switches the memo to PRIVATE while its live push is being delivered: the member
    already pushed keeps theirs, every member after the switch hears nothing of it."""
    _sql(
        "UPDATE users SET notify_on_household_posts = true WHERE id = :u",
        {"u": household.ids["member_off"]},
    )  # a second opted-in member, delivered to after `member_on` (ascending id)
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _files(tmp_path, household.ids["owner"], mid, 1)

    def made_private_after_the_first_member(**kw) -> None:
        pushes(**kw)
        if kw["subscription_info"]["endpoint"] == household.endpoint("member_on"):
            # The edit commits between two recipients, exactly as PATCH would commit it.
            _sql("UPDATE memos SET visibility = 'PRIVATE' WHERE id = :m", {"m": mid})

    monkeypatch.setattr(pywebpush, "webpush", made_private_after_the_first_member)

    asyncio.run(_drain())

    assert pushes.live_per_endpoint(household.prefix) == Counter(
        {household.endpoint("owner"): 1, household.endpoint("member_on"): 1}
    )


@pytest.fixture
def member_memo(pushes, logged_in_client, tmp_path):
    """A PROTECTED memo of the logged-in user with one `ready` file and one still `processing`,
    not yet announced; and the household that would hear of it. Yields
    (client, memo uid, ready uid, processing uid, household)."""
    client, subject = logged_in_client
    owner = _sql("SELECT id FROM users WHERE subject = :s", {"s": subject})[0][0]
    h = _Household()
    mid, muid = _memo(owner, "PROTECTED")
    ready, busy = _files(tmp_path, owner, mid, 2)
    _sql("UPDATE attachments SET status = 'ready' WHERE id = :a", {"a": ready})
    _sql("DELETE FROM media_jobs WHERE attachment_id = :a", {"a": ready})
    uid = {a: u for a, u in _sql("SELECT id, uid FROM attachments WHERE memo_id = :m", {"m": mid})}
    try:
        yield client, muid, uid[ready], uid[busy], h
    finally:
        _sql(
            "DELETE FROM media_jobs WHERE attachment_id IN "
            "(SELECT id FROM attachments WHERE owner_id = :u)",
            {"u": owner},
        )
        _sql("DELETE FROM attachments WHERE owner_id = :u", {"u": owner})
        _sql("DELETE FROM memos WHERE owner_id = :u", {"u": owner})
        h.cleanup()


def test_deleting_the_last_in_flight_file_announces_the_memo(pushes, member_memo):
    client, muid, _, busy, h = member_memo
    assert pushes.live(h.prefix) == []  # still waiting on the file

    assert client.delete(f"/api/attachments/{busy}").status_code == 204

    assert [(e, p["uid"], p["attachments"]) for e, p in pushes.live(h.prefix)] == [
        (h.endpoint("member_on"), muid, 1)
    ]


def test_detaching_the_last_in_flight_file_with_patch_announces_the_memo(pushes, member_memo):
    client, muid, ready, _, h = member_memo

    r = client.patch(f"/api/memos/{muid}", json={"attachment_uids": [ready]})

    assert r.status_code == 200, r.text
    assert [a["uid"] for a in r.json()["attachments"]] == [ready]
    assert [(e, p["uid"], p["attachments"]) for e, p in pushes.live(h.prefix)] == [
        (h.endpoint("member_on"), muid, 1)
    ]


def test_deleting_one_of_two_in_flight_files_announces_nothing_until_the_last(
    pushes, member_memo, tmp_path
):
    client, muid, _, busy, h = member_memo
    [(mid, owner)] = _sql("SELECT id, owner_id FROM memos WHERE uid = :u", {"u": muid})
    [other] = _files(tmp_path, owner, mid, 1)
    [(other_uid,)] = _sql("SELECT uid FROM attachments WHERE id = :a", {"a": other})

    assert client.delete(f"/api/attachments/{busy}").status_code == 204
    assert pushes.live(h.prefix) == []  # one file is still in flight

    assert client.delete(f"/api/attachments/{other_uid}").status_code == 204
    assert len(pushes.live(h.prefix)) == 1


def test_a_failing_announcement_never_fails_the_delete(pushes, member_memo, monkeypatch):
    client, muid, _, busy, _h = member_memo

    async def down(db, user_id, event, **_) -> None:
        raise RuntimeError("push backend down")

    monkeypatch.setattr(notify, "CONSUMERS", [down])

    with capture_logs() as logs:
        assert client.delete(f"/api/attachments/{busy}").status_code == 204
    assert _sql("SELECT 1 FROM attachments WHERE uid = :u", {"u": busy}) == []
    # Best-effort, not silent: the lost push is on the record.
    assert [e["error"] for e in logs if e["event"] == "memo_live_notify_failed"] == [
        "push backend down"
    ]


def test_an_edit_that_detaches_nothing_schedules_no_announcement(pushes, member_memo, monkeypatch):
    client, muid, ready, busy, _h = member_memo
    scheduled: list[int] = []

    async def spy(session_factory, memo_id: int) -> None:
        scheduled.append(memo_id)

    monkeypatch.setattr(memo_live, "announce_safely", spy)

    r = client.patch(f"/api/memos/{muid}", json={"content": "Beach day, edited"})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/memos/{muid}", json={"attachment_uids": [ready, busy]})
    assert r.status_code == 200, r.text

    assert scheduled == []


# --- MEMNG-1189: the residual races of the MEMNG-1184 announcement ---


def test_a_memo_made_private_mid_delivery_reaches_no_further_device_of_the_same_member(
    pushes, household, tmp_path, monkeypatch
):
    """A member with two devices. The owner's switch to PRIVATE commits while the push to the
    member's FIRST device is on the wire: that one push is the only one that still carries the memo
    after the commit — the member's second device, whose push would leave after it, hears nothing.
    Before MEMNG-1189 the member's visibility was read once for all of their devices."""
    member = household.endpoint("member_on")
    _sql(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
        "VALUES (:u, :e, 'k', 'a')",
        {"u": household.ids["member_on"], "e": f"{member}-ipad"},
    )
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _files(tmp_path, household.ids["owner"], mid, 1)

    def made_private_during_the_members_first_push(**kw) -> None:
        pushes(**kw)
        if kw["subscription_info"]["endpoint"].startswith(member):
            # The edit commits while this device's push is in flight, as PATCH would commit it.
            _sql("UPDATE memos SET visibility = 'PRIVATE' WHERE id = :m", {"m": mid})

    monkeypatch.setattr(pywebpush, "webpush", made_private_during_the_members_first_push)

    asyncio.run(_drain())

    per_endpoint = pushes.live_per_endpoint(household.prefix)
    assert per_endpoint[household.endpoint("owner")] == 1
    to_member = {e: n for e, n in per_endpoint.items() if e.startswith(member)}
    assert sum(to_member.values()) == 1, to_member  # the in-flight push, and not the second device


def test_the_owners_own_devices_are_never_held_back_by_the_visibility_gate(
    pushes, household, tmp_path, monkeypatch
):
    """The owner hears of their own memo on every device whatever it is switched to mid-delivery:
    the MEMNG-1189 per-device gate is the household's, never the owner's."""
    owner = household.endpoint("owner")
    _sql(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
        "VALUES (:u, :e, 'k', 'a')",
        {"u": household.ids["owner"], "e": f"{owner}-ipad"},
    )
    mid, _ = _memo(household.ids["owner"], "PROTECTED")
    _files(tmp_path, household.ids["owner"], mid, 1)

    def made_private_during_the_owners_first_push(**kw) -> None:
        pushes(**kw)
        live = json.loads(kw["data"]).get("kind") == "memo_live"  # not the file's own push
        if live and kw["subscription_info"]["endpoint"].startswith(owner):
            _sql("UPDATE memos SET visibility = 'PRIVATE' WHERE id = :m", {"m": mid})

    monkeypatch.setattr(pywebpush, "webpush", made_private_during_the_owners_first_push)

    asyncio.run(_drain())

    assert pushes.live_per_endpoint(household.prefix) == Counter({owner: 1, f"{owner}-ipad": 1})


def test_an_attachment_linked_to_a_memo_while_its_delete_is_on_the_way_announces_that_memo(
    pushes, logged_in_client, tmp_path, monkeypatch
):
    """The delete route read the attachment while it was on no memo; before the reclaim takes its
    lock a memo is created around it (the memo's own after-create claim finds the file in flight
    and stands down). The delete then removes that memo's last in-flight file, so the memo is live
    now, and only an announcement scheduled against the memo the reclaim SAW under its lock says
    so — never the one read before it (MEMNG-1189)."""
    client, subject = logged_in_client
    owner = _sql("SELECT id FROM users WHERE subject = :s", {"s": subject})[0][0]
    h = _Household()
    mid, muid = _memo(owner, "PROTECTED")
    [aid] = _files(tmp_path, owner, mid, 1)
    _sql("UPDATE attachments SET memo_id = NULL WHERE id = :a", {"a": aid})  # not yet on the memo
    [(att_uid,)] = _sql("SELECT uid FROM attachments WHERE id = :a", {"a": aid})
    real_reclaim = reclaim.reclaim_row_shielded

    async def linked_before_the_lock(attachment_id, mode, **kw):
        # The memo create commits between the route's read and the reclaim's row lock.
        _sql("UPDATE attachments SET memo_id = :m WHERE id = :a", {"m": mid, "a": aid})
        return await real_reclaim(attachment_id, mode, **kw)

    monkeypatch.setattr(reclaim, "reclaim_row_shielded", linked_before_the_lock)
    try:
        assert client.delete(f"/api/attachments/{att_uid}").status_code == 204

        assert _sql("SELECT 1 FROM attachments WHERE id = :a", {"a": aid}) == []
        assert [(e, p["uid"]) for e, p in pushes.live(h.prefix)] == [
            (h.endpoint("member_on"), muid)
        ]
    finally:
        _sql("DELETE FROM media_jobs WHERE attachment_id = :a", {"a": aid})
        _sql("DELETE FROM attachments WHERE owner_id = :u", {"u": owner})
        _sql("DELETE FROM memos WHERE owner_id = :u", {"u": owner})
        h.cleanup()
