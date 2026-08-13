"""MEMNG-31: a PAT used on the compat surface leaves last_used durably updated.

Before this, `auth.authenticate_pat` only FLUSHED the stamp, and the compat surface never commits
(routers/compat.py `optional_user`, and `serve_file_compat`'s own short-lived session) — so the
transaction was rolled back at request end and the row never changed. The stamp now lives in
`auth.stamp_pat_last_used`: throttled to one write per window per PAT, and committed in its own
short transaction so a satellite's gallery burst neither rewrites the row per request nor holds
that row's lock for the rest of the request.

Every read-back here opens a FRESH engine + connection, so it can only observe COMMITTED state —
that is what makes these tests fail if the commit goes away.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from app import auth
from app.config import settings
from app.database import async_session
from app.models import Pat
from PIL import Image
from sqlalchemy import create_engine, select, text

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
                "INSERT INTO pats (hashed_token, name, user_id) "
                "VALUES (:h,'sat',:u) RETURNING id"
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


def test_compat_list_request_persists_last_used(client, db_engine):
    """AC1 + AC2 on /api/v1/memos: the request-scoped session is never committed by the router."""
    pat_id = _seed()
    try:
        assert _last_used(pat_id) is None
        before = datetime.now(UTC)
        assert client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        stamped = _last_used(pat_id)
        assert stamped is not None
        assert stamped >= before - timedelta(seconds=5)
    finally:
        _cleanup()


def test_compat_file_request_persists_last_used(client, db_engine, monkeypatch, tmp_path):
    """AC1 + AC2 on /file/*: that route authorizes in its OWN session, which is closed (→ rolled
    back) before the file is streamed — the stamp has to be committed by then or it is lost."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _thumb(tmp_path)
    pat_id = _seed()
    try:
        assert _last_used(pat_id) is None
        r = client.get("/file/attachments/patlu-img/IMG.jpeg?thumbnail=true", headers=HEADERS)
        assert r.status_code == 200
        assert _last_used(pat_id) is not None
    finally:
        _cleanup()


def test_clean_api_pat_request_still_persists_last_used(client, db_engine, monkeypatch, tmp_path):
    """The clean API's PAT path (require_pat_or_user → /api/attachments/{uid}/original) keeps
    persisting the stamp now that its own commit is gone and authenticate_pat owns durability."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    orig = tmp_path / "orig.jpg"
    Image.new("RGB", (12, 12), (4, 5, 6)).save(orig, "JPEG")
    pat_id = _seed(storage_path=str(orig))
    try:
        assert _last_used(pat_id) is None
        r = client.get("/api/attachments/patlu-img/original", headers=HEADERS)
        assert r.status_code == 200
        assert _last_used(pat_id) is not None
    finally:
        _cleanup()


def test_second_request_inside_the_window_does_not_rewrite(client, db_engine, monkeypatch):
    """Throttle: one write per window per PAT, then a fresh write once the window has passed."""
    pat_id = _seed()
    try:
        assert client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        first = _last_used(pat_id)
        assert first is not None

        assert client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        assert _last_used(pat_id) == first  # inside the window: the row is untouched

        monkeypatch.setattr(settings, "PAT_LAST_USED_THROTTLE_S", 0)  # window elapsed
        assert client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        second = _last_used(pat_id)
        assert second is not None and second > first
    finally:
        _cleanup()


def test_a_process_without_the_in_process_claim_still_does_not_rewrite(client, db_engine):
    """The claim map is per-process and non-durable (a second worker, a restart, or the >4096
    bound clearing it). The durable gates — the freshness check on the loaded row and the same
    predicate in the UPDATE's WHERE — must then hold the throttle on their own."""
    pat_id = _seed()
    try:
        assert client.get("/api/v1/memos", headers=HEADERS).status_code == 200
        first = _last_used(pat_id)
        assert first is not None
        auth._pat_last_used_claimed.clear()  # a fresh process has no claim for this PAT
        assert client.get("/api/v1/memos", headers=HEADERS).status_code == 200
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


async def test_concurrent_burst_attempts_exactly_one_write(db_engine):
    """The folded MEMNG-25 finding: a gallery burst on one PAT must not have every request take
    that row's lock. The claim is made synchronously before the UPDATE, so exactly one coroutine
    of the burst attempts the write and the rest return without touching the row."""
    pat_id = _seed()
    try:

        async def one() -> bool:
            async with async_session() as db:
                pat = (await db.execute(select(Pat).where(Pat.id == pat_id))).scalar_one()
                return await auth.stamp_pat_last_used(pat.id, pat.last_used)

        results = await asyncio.gather(*(one() for _ in range(8)))
        assert sum(results) == 1
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


def test_a_failing_stamp_never_breaks_pat_authentication(client, db_engine, monkeypatch):
    # The stamp is bookkeeping in its own dedicated session; a canceled UPDATE on
    # the locked PAT row (or any transient DB error inside it) fails that session
    # alone and must not turn a valid token into a 500 or expire loaded state.
    pat_id = _seed()
    try:
        async def _boom(db, pat):
            raise RuntimeError("stamp exploded")

        monkeypatch.setattr(auth, "stamp_pat_last_used", _boom)
        r = client.get("/api/v1/memos", headers=HEADERS)
        assert r.status_code == 200
        assert _last_used(pat_id) is None
    finally:
        _cleanup()
