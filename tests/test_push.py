import json

import pytest
import pywebpush
from app import worker
from app.config import settings
from app.models import Attachment, AttachmentStatus, PushSubscription, User
from app.services import push
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL


async def _make_user(adb, subject: str) -> User:
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


# --- HTTP: subscribe / vapid key ---


def test_subscribe_requires_auth(client):
    r = client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://x/1", "keys": {"p256dh": "k", "auth": "a"}},
    )
    assert r.status_code == 401


def test_vapid_public_key(client, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "PUBKEY123")
    assert client.get("/api/push/vapid-public-key").json()["key"] == "PUBKEY123"


def test_subscribe_stores_and_upserts_by_endpoint(logged_in_client):
    client, _ = logged_in_client
    endpoint = "https://push.example/abc"
    first = {"endpoint": endpoint, "keys": {"p256dh": "key1", "auth": "auth1"}}
    assert client.post("/api/push/subscribe", json=first).status_code == 204
    # Re-subscribe to the SAME endpoint with rotated keys → upsert, not a second row.
    second = {"endpoint": endpoint, "keys": {"p256dh": "key2", "auth": "auth2"}}
    assert client.post("/api/push/subscribe", json=second).status_code == 204

    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        rows = c.execute(
            text("SELECT p256dh FROM push_subscriptions WHERE endpoint = :e"), {"e": endpoint}
        ).all()
    eng.dispose()
    assert len(rows) == 1 and rows[0][0] == "key2"  # single row, keys rotated


def test_unsubscribe_removes_row(logged_in_client):
    client, _ = logged_in_client
    endpoint = "https://push.example/del"
    body = {"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post("/api/push/subscribe", json=body).status_code == 204
    assert client.request("DELETE", "/api/push/subscribe", json=body).status_code == 204

    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        n = c.execute(
            text("SELECT count(*) FROM push_subscriptions WHERE endpoint = :e"), {"e": endpoint}
        ).scalar_one()
    eng.dispose()
    assert n == 0


# --- service: send_to_user fan-out + pruning ---


async def test_send_to_user_calls_webpush_per_subscription(adb, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "PRIVKEY")
    u = await _make_user(adb, "push_unit_user")
    adb.add_all(
        [
            PushSubscription(user_id=u.id, endpoint="https://p/1", p256dh="a", auth="b"),
            PushSubscription(user_id=u.id, endpoint="https://p/2", p256dh="c", auth="d"),
        ]
    )
    await adb.flush()

    calls: list[dict] = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: calls.append(kw))

    sent = await push.send_to_user(adb, u.id, {"uid": "x", "status": "ready"})
    assert sent == 2 and len(calls) == 2
    assert all(json.loads(c["data"])["status"] == "ready" for c in calls)
    assert {c["subscription_info"]["endpoint"] for c in calls} == {"https://p/1", "https://p/2"}


async def test_send_to_user_prunes_gone_endpoint(adb, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "PRIVKEY")
    u = await _make_user(adb, "push_prune_user")
    adb.add(PushSubscription(user_id=u.id, endpoint="https://dead/1", p256dh="a", auth="b"))
    await adb.flush()

    class _Resp:
        status_code = 410

    def _gone(**kw):
        raise pywebpush.WebPushException("gone", response=_Resp())

    monkeypatch.setattr(pywebpush, "webpush", _gone)

    sent = await push.send_to_user(adb, u.id, {"uid": "x", "status": "ready"})
    remaining = (
        (await adb.execute(select(PushSubscription).where(PushSubscription.user_id == u.id)))
        .scalars()
        .all()
    )
    assert sent == 0 and remaining == []  # 410 → endpoint pruned


async def test_send_to_user_keeps_subscription_on_transient_error(adb, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "PRIVKEY")
    u = await _make_user(adb, "push_transient_user")
    adb.add(PushSubscription(user_id=u.id, endpoint="https://flaky/1", p256dh="a", auth="b"))
    await adb.flush()

    class _Resp:
        status_code = 503  # transient — NOT a dead endpoint

    def _boom(**kw):
        raise pywebpush.WebPushException("temporarily unavailable", response=_Resp())

    monkeypatch.setattr(pywebpush, "webpush", _boom)

    await push.send_to_user(adb, u.id, {"uid": "x"})
    remaining = (
        (await adb.execute(select(PushSubscription).where(PushSubscription.user_id == u.id)))
        .scalars()
        .all()
    )
    assert len(remaining) == 1  # transient error must not prune


async def test_send_to_user_noop_without_vapid(adb, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    u = await _make_user(adb, "push_noop_user")
    adb.add(PushSubscription(user_id=u.id, endpoint="https://p/1", p256dh="a", auth="b"))
    await adb.flush()

    called: list[dict] = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: called.append(kw))

    sent = await push.send_to_user(adb, u.id, {"uid": "x"})
    assert sent == 0 and called == []  # VAPID unset → push disabled


async def _three_subscriptions(adb, monkeypatch, subject: str) -> tuple[int, list[dict]]:
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "PRIVKEY")
    u = await _make_user(adb, subject)
    adb.add_all(
        [
            PushSubscription(user_id=u.id, endpoint=f"https://p/{i}", p256dh="a", auth="b")
            for i in range(3)
        ]
    )
    await adb.flush()
    calls: list[dict] = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: calls.append(kw))
    return u.id, calls


async def test_send_to_user_asks_still_wanted_before_every_send_the_first_included(
    adb, monkeypatch
):
    """MEMNG-1189: the gate is asked before EACH subscription — before the first one too, and the
    first False stops every send still to come."""
    user_id, calls = await _three_subscriptions(adb, monkeypatch, "push_gate_user")
    answers = iter([True, False, True])
    asked: list[int] = []

    async def still_wanted() -> bool:
        asked.append(len(calls))  # how many had left when it was asked
        return next(answers)

    sent = await push.send_to_user(adb, user_id, {"uid": "x"}, still_wanted=still_wanted)
    assert (sent, len(calls), asked) == (1, 1, [0, 1])

    async def never() -> bool:
        return False

    calls.clear()
    assert await push.send_to_user(adb, user_id, {"uid": "x"}, still_wanted=never) == 0
    assert calls == []


async def test_send_to_user_a_failing_still_wanted_sends_nothing(adb, monkeypatch):
    """A gate that cannot answer (its read failed) is not a yes: the error reaches the caller and
    nothing is sent on the strength of it (MEMNG-1189)."""
    user_id, calls = await _three_subscriptions(adb, monkeypatch, "push_gate_err_user")

    async def broken() -> bool:
        raise RuntimeError("visibility read failed")

    with pytest.raises(RuntimeError, match="visibility read failed"):
        await push.send_to_user(adb, user_id, {"uid": "x"}, still_wanted=broken)
    assert calls == []


# --- worker hook: notify only on a terminal attachment state ---


async def test_worker_notifies_on_ready(adb, monkeypatch):
    u = await _make_user(adb, "push_worker_ready")
    att = Attachment(
        uid="wkr-ready",
        owner_id=u.id,
        filename="x.jpg",
        mime="image/jpeg",
        size=1,
        status=AttachmentStatus.ready,
    )
    adb.add(att)
    await adb.flush()

    calls: list[tuple] = []

    async def _fake_send(db, user_id, payload, **_):
        calls.append((user_id, payload))
        return 1

    monkeypatch.setattr(worker.push, "send_to_user", _fake_send)
    await worker._notify_terminal(adb, att.id)
    assert len(calls) == 1
    assert calls[0][0] == u.id
    assert calls[0][1]["status"] == "ready" and calls[0][1]["uid"] == "wkr-ready"


async def test_worker_skips_notify_while_processing(adb, monkeypatch):
    u = await _make_user(adb, "push_worker_proc")
    att = Attachment(
        uid="wkr-proc",
        owner_id=u.id,
        filename="v.mov",
        mime="video/quicktime",
        size=1,
        status=AttachmentStatus.processing,
    )
    adb.add(att)
    await adb.flush()

    calls: list = []

    async def _fake_send(db, user_id, payload, **_):
        calls.append(payload)
        return 1

    monkeypatch.setattr(worker.push, "send_to_user", _fake_send)
    await worker._notify_terminal(adb, att.id)
    assert calls == []  # video still transcoding (processing) → no premature notify


async def test_notify_terminal_safe_swallows_errors(monkeypatch):
    # The job is already committed before push runs; a push/session error must be swallowed,
    # never propagated back into the worker's job loop.
    async def _boom(db, attachment_id):
        raise RuntimeError("push backend down")

    monkeypatch.setattr(worker, "_notify_terminal", _boom)
    await worker._notify_terminal_safe(999999)  # must not raise
