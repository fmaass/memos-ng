import io
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app import worker
from app.config import settings
from app.database import async_session
from app.models import (
    Attachment,
    AttachmentStatus,
    FrameShareJob,
    FrameShareState,
    Tag,
    User,
    Visibility,
)
from app.services import frame_share as fs
from app.services import memos as memos_svc
from PIL import Image
from sqlalchemy import delete, select

# --- render_for_frame (pure, no DB) ---


def _write_jpeg(path, size=(120, 80), color=(30, 60, 90), orientation=None, dto=None):
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    if orientation is not None:
        exif[0x0112] = orientation
    if dto is not None:
        exif.get_ifd(0x8769)[0x9003] = dto
    img.save(path, "JPEG", exif=exif)


def test_render_small_image_not_upscaled(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p, (140, 90))
    im = Image.open(io.BytesIO(fs.render_for_frame(str(p))))
    assert im.format == "JPEG"
    assert im.size == (140, 90)  # below the cap → unchanged, never upscaled


def test_render_large_image_downscaled_to_2048_longest_edge(tmp_path):
    # Real curated exports are ~8600px; render must cap the longest edge so the JPEG stays under
    # iCloud's 20MB / Aura's 9.5MB limits (regression: full-res rendered 20-46MB and iCloud 552'd).
    p = tmp_path / "big.jpg"
    _write_jpeg(p, (6000, 4000))
    out = fs.render_for_frame(str(p))
    im = Image.open(io.BytesIO(out))
    assert max(im.size) == fs.FRAME_MAX_EDGE == 2048  # longest edge capped
    assert im.size == (2048, 1365)  # aspect ratio preserved
    assert len(out) < 9 * 1024 * 1024  # safely under Aura's proxy cap


def test_render_backfills_datetimeoriginal_from_captured_at(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p)  # no EXIF DateTimeOriginal
    out = fs.render_for_frame(str(p), datetime(2024, 3, 1, 12, 30, 0, tzinfo=UTC))
    dto = Image.open(io.BytesIO(out)).getexif().get_ifd(0x8769).get(0x9003)
    assert dto == "2024:03:01 12:30:00"


def test_render_preserves_existing_datetimeoriginal(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p, dto="2020:01:02 03:04:05")
    out = fs.render_for_frame(str(p), datetime(2024, 3, 1, tzinfo=UTC))
    dto = Image.open(io.BytesIO(out)).getexif().get_ifd(0x8769).get(0x9003)
    assert dto == "2020:01:02 03:04:05"  # source EXIF wins; captured_at does not overwrite


def test_render_normalizes_orientation(tmp_path):
    p = tmp_path / "o.jpg"
    _write_jpeg(p, size=(100, 60), orientation=6)  # 6 = rotate 90° CW
    im = Image.open(io.BytesIO(fs.render_for_frame(str(p))))
    assert im.getexif().get(0x0112, 1) == 1  # tag reset
    assert im.size == (60, 100)  # pixels physically rotated by exif_transpose


def test_render_heic_source_to_jpeg(tmp_path):
    pytest.importorskip("pillow_heif")
    p = tmp_path / "o.heic"
    try:
        Image.new("RGB", (80, 50), (5, 5, 5)).save(p, format="HEIF")
    except (OSError, KeyError):
        pytest.skip("HEIF encoder unavailable in this pillow-heif build")
    assert Image.open(io.BytesIO(fs.render_for_frame(str(p)))).format == "JPEG"


# --- album HTTP upload helpers (Phase 2 send path) ---

FRAME_API_URL = "http://frame.local"
FRAME_ALBUM_ID = 7
FRAME_API_TOKEN = "album-token"


def _frame_api_settings(
    monkeypatch, *, url=FRAME_API_URL, album=FRAME_ALBUM_ID, token=FRAME_API_TOKEN
):
    monkeypatch.setattr(settings, "FRAME_API_URL", url)
    monkeypatch.setattr(settings, "FRAME_ALBUM_ID", album)
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", token)


def _mock_album(monkeypatch, *, status=201, exc=None, captured=None):
    """Route upload_to_album's POST through an httpx.MockTransport that returns `status` (or whose
    handler raises `exc` to simulate a network/timeout error). Each request is appended to
    `captured` (a list) so callers can assert the URL/headers/body. Does NOT touch settings — the
    caller decides whether the feature is configured."""

    def handler(request):
        if captured is not None:
            captured.append(request)
        if exc is not None:
            raise exc
        return httpx.Response(status)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)


@pytest.mark.parametrize("status", [200, 201])
async def test_upload_to_album_2xx_succeeds(monkeypatch, status):
    # 201 = new photo, 200 = SHA-duplicate — both mean the photo is safely in the album, so
    # upload_to_album returns without raising for either.
    _frame_api_settings(monkeypatch)
    _mock_album(monkeypatch, status=status)
    assert await fs.upload_to_album("x.jpg", b"\xff\xd8\xff\xd9") is None


async def test_upload_to_album_error_raises_with_status_code(monkeypatch):
    _frame_api_settings(monkeypatch)
    _mock_album(monkeypatch, status=422)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await fs.upload_to_album("x.jpg", b"\xff\xd8\xff\xd9")
    assert ei.value.response.status_code == 422  # status carried for the caller's classification


async def test_upload_to_album_posts_bearer_and_filename(monkeypatch):
    _frame_api_settings(monkeypatch)
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    await fs.upload_to_album("sunset.jpg", b"\xff\xd8body\xff\xd9")
    req = captured[0]
    assert str(req.url) == f"{FRAME_API_URL}/api/albums/{FRAME_ALBUM_ID}/photos"
    assert req.headers["Authorization"] == f"Bearer {FRAME_API_TOKEN}"
    assert req.headers["Content-Type"] == "image/jpeg"
    assert req.headers["X-Filename"] == "sunset.jpg"
    assert req.content == b"\xff\xd8body\xff\xd9"  # raw JPEG bytes are the body


async def test_upload_to_album_uses_explicit_30s_timeout(monkeypatch):
    # The explicit httpx.Timeout(30.0) keeps a hung server from stalling the worker drain. Capture
    # the AsyncClient construction kwargs and pin it — this goes red if the explicit timeout is
    # dropped (the arg would be absent → recorded None → not an httpx.Timeout).
    _frame_api_settings(monkeypatch)
    recorded = {}
    orig = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(201))

    def factory(*args, **kwargs):
        recorded["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)
    await fs.upload_to_album("x.jpg", b"\xff\xd8\xff\xd9")
    t = recorded["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.connect == t.read == t.write == t.pool == 30.0


async def test_upload_to_album_ascii_encodes_non_ascii_filename(monkeypatch):
    # httpx 0.28 rejects non-ASCII header values — a curated photo named café.jpg would raise
    # UnicodeEncodeError. The header value must be percent-encoded (deterministic), ASCII, and keep
    # the .jpg extension.
    _frame_api_settings(monkeypatch)
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    await fs.upload_to_album("café.jpg", b"\xff\xd8\xff\xd9")  # must not raise
    xf = captured[0].headers["X-Filename"]
    assert xf == "caf%C3%A9.jpg" and xf.isascii() and xf.endswith(".jpg")


def _status_err(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", f"{FRAME_API_URL}/api/albums/{FRAME_ALBUM_ID}/photos")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


@pytest.mark.parametrize(
    "exc,expected",
    [
        # transient → retry with backoff
        (httpx.TimeoutException("t"), True),
        (httpx.ConnectError("c"), True),
        (httpx.ReadError("r"), True),
        (httpx.RemoteProtocolError("malformed server response"), True),  # server-side → transient
        (_status_err(408), True),
        (_status_err(429), True),
        (_status_err(500), True),
        (_status_err(502), True),
        (_status_err(503), True),
        # permanent → terminal failure
        # LocalProtocolError = a malformed OUTGOING request (bad token) — a TransportError subclass
        # that MUST classify permanent (checked before the broad TransportError branch).
        (httpx.LocalProtocolError("Illegal header value b'Bearer x'"), False),
        (_status_err(400), False),
        (_status_err(401), False),
        (_status_err(403), False),
        (_status_err(404), False),
        (_status_err(413), False),
        (_status_err(415), False),
        (_status_err(422), False),
        (ValueError("not an httpx error"), False),
    ],
)
def test_is_transient_http_error(exc, expected):
    assert fs._is_transient_http_error(exc) is expected


# --- process_claimed_job state machine (adb; the worker owns commits, so no commit here) ---


async def _make_claimed(
    adb, monkeypatch, tmp_path, *, content="pics #curated", mime="image/jpeg",
    write_file=True, configured=True, subject=None, filename="photo.jpg",
):
    if configured:
        _frame_api_settings(monkeypatch)
    u = User(
        subject=subject or f"u-{secrets.token_hex(4)}", role="user",
        frame_send_to="frame@send.auraframes.com", frame_send_from="sender@me.com",
    )
    adb.add(u)
    await adb.flush()
    storage = None
    if write_file:
        f = tmp_path / f"{secrets.token_hex(4)}.jpg"
        _write_jpeg(f, (64, 48))
        storage = str(f)
    att = Attachment(
        uid=secrets.token_urlsafe(8), owner_id=u.id, filename=filename, mime=mime,
        size=1, status=AttachmentStatus.ready, storage_path=storage,
        captured_at=datetime(2024, 5, 5, tzinfo=UTC),
    )
    adb.add(att)
    await adb.flush()
    memo = await memos_svc.create_memo(
        adb, u, content=content, visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
    )
    job = FrameShareJob(
        attachment_id=att.id, memo_id=memo.id, user_id=u.id,
        state=FrameShareState.claimed, attempts=0,
    )
    adb.add(job)
    await adb.flush()
    return job, att, memo, u


async def test_process_new_upload_201_marks_sent(adb, monkeypatch, tmp_path):
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "sent"
    assert job.state is FrameShareState.sent and job.sent_at is not None
    assert len(captured) == 1  # exactly one upload


async def test_process_duplicate_200_marks_sent(adb, monkeypatch, tmp_path):
    # A SHA-duplicate returns 200 — the photo is already in the album, so this is SUCCESS, not a
    # failure. The job must end `sent` (idempotent), never `failed`.
    _mock_album(monkeypatch, status=200)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "sent"
    assert job.state is FrameShareState.sent and job.sent_at is not None


async def test_process_stamps_memo_date_not_captured_at(adb, monkeypatch, tmp_path):
    # Frame ordering key is the MEMO's date, not attachment.captured_at (unreliable for exports).
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    _frame_api_settings(monkeypatch)
    u = User(
        subject=f"u-{secrets.token_hex(4)}", role="user",
        frame_send_to="f@send.auraframes.com", frame_send_from="s@me.com",
    )
    adb.add(u)
    await adb.flush()
    src = tmp_path / "src.png"
    Image.new("RGB", (50, 40), (1, 2, 3)).save(src, "PNG")  # PNG source → no EXIF
    att = Attachment(
        uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/png",
        size=1, status=AttachmentStatus.ready, storage_path=str(src),
        captured_at=datetime(2020, 1, 1, tzinfo=UTC),  # deliberately WRONG/old
    )
    adb.add(att)
    await adb.flush()
    memo = await memos_svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid],
        created_at=datetime(2026, 5, 12, 9, 30, 0, tzinfo=UTC),  # the intended frame date
    )
    job = FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                        state=FrameShareState.claimed, attempts=0)
    adb.add(job)
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "sent"
    im = Image.open(io.BytesIO(captured[0].content))  # the uploaded JPEG bytes
    dto = im.getexif().get_ifd(0x8769).get(0x9003)
    assert dto == "2026:05:12 09:30:00"  # memo date, NOT captured_at's 2020


async def test_process_500_retries_with_backoff(adb, monkeypatch, tmp_path):
    # A 5xx is a transient server-side error → retry with backoff (queued + future next_attempt_at).
    _mock_album(monkeypatch, status=500)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    before = datetime.now(UTC)
    assert await fs.process_claimed_job(adb, job) == "retry"
    assert job.state is FrameShareState.queued
    assert job.attempts == 1 and job.next_attempt_at is not None
    assert job.next_attempt_at > before  # backoff scheduled into the future


async def test_process_transient_terminal_at_max_attempts(adb, monkeypatch, tmp_path):
    # Even a transient (5xx) failure goes terminal once attempts hit FRAME_MAX_ATTEMPTS.
    _mock_album(monkeypatch, status=503)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    job.attempts = 4  # next failure is the 5th
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "failed"
    assert job.state is FrameShareState.failed and job.attempts == 5


async def test_process_422_fails_immediately(adb, monkeypatch, tmp_path):
    # A 422 (and any non-408/429 4xx) is a permanent client error retrying can't fix → terminal
    # `failed` on the FIRST attempt, no churn through all five, no backoff scheduled.
    _mock_album(monkeypatch, status=422)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "failed"
    assert job.state is FrameShareState.failed and job.attempts == 1
    assert job.next_attempt_at is None  # no retry scheduled


@pytest.mark.parametrize(
    "exc",
    [httpx.TimeoutException("timed out"), httpx.ConnectError("connection refused")],
)
async def test_process_network_error_retries(adb, monkeypatch, tmp_path, exc):
    # A timeout / connect error is transient → retry with backoff (queued, attempts incremented).
    _mock_album(monkeypatch, exc=exc)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert await fs.process_claimed_job(adb, job) == "retry"
    assert job.state is FrameShareState.queued
    assert job.attempts == 1 and job.next_attempt_at is not None


async def test_process_render_failure_fails_immediately(adb, monkeypatch, tmp_path):
    # A corrupt/undecodable original makes render_for_frame raise. That is a permanent data problem,
    # NOT a transient send error — it must go terminal `failed` on the FIRST attempt, never churn
    # through all five with backoff, and never reach the upload step.
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, att, *_ = await _make_claimed(adb, monkeypatch, tmp_path, write_file=False)
    bad = tmp_path / "corrupt.jpg"
    bad.write_bytes(b"this is not a decodable image")
    att.storage_path = str(bad)
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "failed"
    assert job.state is FrameShareState.failed
    assert job.attempts == 1  # terminal on first attempt, no 5-attempt churn
    assert job.next_attempt_at is None  # no retry scheduled
    assert captured == []  # never reached the upload step
    assert job.error  # the decode error is recorded


async def test_process_uncurated_skips_without_sending(adb, monkeypatch, tmp_path):
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, content="no tag here")
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and captured == []


async def test_process_unlinked_attachment_skips(adb, monkeypatch, tmp_path):
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, att, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    att.memo_id = None  # attachment unlinked before the worker ran
    await adb.flush()
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and captured == []


async def test_process_missing_original_file_skips(adb, monkeypatch, tmp_path):
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, write_file=False)
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and captured == []


async def test_process_frame_api_unset_skips(adb, monkeypatch, tmp_path):
    # The album API is not configured → send-time revalidation returns None → the job is skipped
    # and nothing is uploaded (mirrors the enqueue guard for a feature disabled mid-flight).
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, configured=False)
    _frame_api_settings(monkeypatch, url="", album=0, token="")
    assert await fs.process_claimed_job(adb, job) == "skipped"
    assert job.state is FrameShareState.skipped and captured == []


async def test_process_non_ascii_filename_uploads_ascii_header(adb, monkeypatch, tmp_path):
    # A curated photo can have a non-ASCII name (café.jpg). httpx 0.28 rejects non-ASCII header
    # values, so an un-encoded X-Filename would fail the upload permanently on attempt 1 — a
    # regression vs the old email path. The header must be ASCII-safe and the job still `sent`.
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path, filename="café.jpg")
    assert await fs.process_claimed_job(adb, job) == "sent"
    xf = captured[0].headers["X-Filename"]
    assert xf.isascii() and xf and xf.endswith(".jpg")  # ASCII-only, non-empty, keeps .jpg
    assert xf == "caf%C3%A9.jpg"  # deterministic percent-encoding


async def test_process_bad_token_fails_permanently_without_leaking(adb, monkeypatch, tmp_path):
    # A token with an illegal header char makes httpx raise LocalProtocolError whose message embeds
    # the full `Bearer <token>` value — and an INTERNAL control char (\n here) is escaped by h11's
    # bytes-repr, so string redaction of the raw/stripped token would NOT catch it. Two invariants:
    # (1) it is PERMANENT (a config error retrying can't fix) → `failed`, not retried; (2) the error
    # is synthesized from the exception type only, so no fragment of the token can ever appear.
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)  # sets a valid url/album/token
    secret = "SENTINEL\nTOKEN"  # internal control char — the case redaction cannot handle
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", secret)
    # Faithful to h11: the message carries the header value via bytes-repr (escapes the \n).
    header_repr = repr(f"Bearer {secret}".encode())
    exc = httpx.LocalProtocolError(f"Illegal header value {header_repr}")
    _mock_album(monkeypatch, exc=exc)
    assert await fs.process_claimed_job(adb, job) == "failed"  # permanent, NOT retried
    assert job.state is FrameShareState.failed and job.attempts == 1
    assert job.next_attempt_at is None  # no backoff scheduled — never retried
    assert job.error == "upload failed: LocalProtocolError"  # synthesized from type only
    # Neither the raw token nor any non-trivial fragment of it may appear in the stored error.
    assert "SENTINEL" not in (job.error or "") and "TOKEN" not in (job.error or "")


# --- full flow: enqueue via the real guard → claim → upload → sent ---


async def test_full_flow_enqueue_then_upload_marks_sent(adb, monkeypatch, tmp_path):
    _frame_api_settings(monkeypatch)
    captured = []
    _mock_album(monkeypatch, status=201, captured=captured)
    u = User(subject=f"u-{secrets.token_hex(4)}", role="user")  # no per-user frame settings needed
    adb.add(u)
    await adb.flush()
    src = tmp_path / "sunset.png"
    Image.new("RGB", (120, 80), (10, 20, 30)).save(src, "PNG")
    att = Attachment(
        uid=secrets.token_urlsafe(8), owner_id=u.id, filename="sunset.png", mime="image/png",
        size=1, status=AttachmentStatus.ready, storage_path=str(src),
    )
    adb.add(att)
    await adb.flush()
    memo = await memos_svc.create_memo(
        adb, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
    )
    # Enqueue through the real guard (frame_api_configured, no per-user settings).
    assert await fs.maybe_enqueue_frame_shares(adb, memo, u) == 1
    job = (
        await adb.execute(select(FrameShareJob).where(FrameShareJob.memo_id == memo.id))
    ).scalar_one()
    job.state = FrameShareState.claimed  # the worker claims before processing
    await adb.flush()

    assert await fs.process_claimed_job(adb, job) == "sent"
    assert job.state is FrameShareState.sent and job.sent_at is not None
    req = captured[0]
    assert str(req.url) == f"{FRAME_API_URL}/api/albums/{FRAME_ALBUM_ID}/photos"
    assert req.headers["Authorization"] == f"Bearer {FRAME_API_TOKEN}"
    assert req.headers["X-Filename"] == "sunset.jpg"  # attachment filename stem + .jpg


# --- run_frame_share_drain isolates errors from the worker loop (media stays alive) ---


async def test_frame_share_drain_swallows_errors(monkeypatch, db_engine):
    # A raise out of run_frame_share_once (a DB error in claim/revalidate/commit) MUST NOT escape
    # the drain — if it did, run()'s loop would crash and media processing would halt. The drain
    # converts it to a non-processed 'error' status and continues.
    async def boom(db):
        raise RuntimeError("db exploded mid-claim")

    monkeypatch.setattr(worker, "run_frame_share_once", boom)
    assert await worker.run_frame_share_drain() == "error"


# --- claim honors state + next_attempt_at ---


async def test_claim_frame_share_honors_state_and_next_attempt(adb, monkeypatch, tmp_path):
    due, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    future, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    already_sent, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    # Reset states: `due` is queued+due, `future` is queued but not yet due, `already_sent` is sent.
    due.state = FrameShareState.queued
    future.state = FrameShareState.queued
    future.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
    already_sent.state = FrameShareState.sent
    await adb.flush()
    claimed = await worker.claim_frame_share_one(adb)
    assert claimed is not None and claimed.id == due.id
    assert claimed.state is FrameShareState.claimed and claimed.claimed_by is not None


# --- integration: run_frame_share_once end-to-end with real commits (at-most-once path) ---


async def test_run_frame_share_once_end_to_end_marks_sent(monkeypatch, tmp_path, db_engine):
    captured = []
    _frame_api_settings(monkeypatch)
    _mock_album(monkeypatch, status=201, captured=captured)
    f = tmp_path / "real.jpg"
    _write_jpeg(f, (100, 70))
    subject = f"e2e-{secrets.token_hex(4)}"
    user_id = None
    try:
        async with async_session() as db:
            u = User(
                subject=subject, role="user",
                frame_send_to="frame@send.auraframes.com", frame_send_from="sender@me.com",
            )
            db.add(u)
            await db.flush()
            user_id = u.id
            att = Attachment(
                uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/jpeg",
                size=1, status=AttachmentStatus.ready, storage_path=str(f),
            )
            db.add(att)
            await db.flush()
            memo = await memos_svc.create_memo(
                db, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
            )
            db.add(FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                                 state=FrameShareState.queued, attempts=0))
            await db.commit()

        async with async_session() as db:
            assert await worker.run_frame_share_once(db) == "processed"

        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            assert job.state is FrameShareState.sent and job.sent_at is not None
        assert len(captured) == 1
    finally:
        # This test COMMITS (unlike adb-rolled-back tests), so it must remove everything it wrote.
        # Deleting the user cascades to memo/attachment/job, but the shared `tags` row does not
        # cascade — a leftover 'curated' tag would collide with test_models' UNIQUE Tag insert.
        async with async_session() as db:
            if user_id is not None:
                await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tag).where(Tag.name == "curated"))
            await db.commit()


async def test_crash_after_claim_commit_is_recovered_and_reclaimable(
    monkeypatch, tmp_path, db_engine
):
    # MEMNG-35, the exact stranding window: the worker COMMITS the `claimed` tombstone and then dies
    # before the upload finishes. Drives the real claim path (which commits), abandons the session
    # like a SIGKILL would, ages the lease past the horizon, and asserts through FRESH sessions that
    # the job is queued, attempt-counted and claimable again — the delivery resumes instead of
    # sitting in `claimed` forever. Before the fix nothing ever reset that row.
    _frame_api_settings(monkeypatch)
    f = tmp_path / "stranded.jpg"
    _write_jpeg(f, (100, 70))
    subject = f"crash-{secrets.token_hex(4)}"
    user_id = None
    stale = timedelta(minutes=settings.FRAME_CLAIM_STALE_MINUTES + 1)
    try:
        async with async_session() as db:
            u = User(subject=subject, role="user")
            db.add(u)
            await db.flush()
            user_id = u.id
            att = Attachment(
                uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/jpeg",
                size=1, status=AttachmentStatus.ready, storage_path=str(f),
            )
            db.add(att)
            await db.flush()
            memo = await memos_svc.create_memo(
                db, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
            )
            db.add(FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                                 state=FrameShareState.queued, attempts=0))
            await db.commit()

        # The crash: claim + commit the tombstone (worker.run_frame_share_once's commit BEFORE the
        # external send), then never process it.
        async with async_session() as db:
            claimed = await worker.claim_frame_share_one(db)
            assert claimed is not None and claimed.state is FrameShareState.claimed
            await db.commit()

        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            assert job.state is FrameShareState.claimed
            assert job.claimed_at is not None  # the lease the sweep reads
        assert await fs.recover_stale_claimed_committed() == 0  # still fresh → left alone

        # Age the lease: the crash was longer ago than the horizon.
        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            job.claimed_at = datetime.now(UTC) - stale
            await db.commit()

        assert await fs.recover_stale_claimed_committed() == 1

        # Fresh session: read the committed truth back, not a stale in-memory identity.
        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            assert job.state is FrameShareState.queued
            assert job.claimed_by is None and job.claimed_at is None
            assert job.attempts == 1  # recovery costs an attempt → a poison job can't loop forever
            assert job.next_attempt_at is None  # due immediately; a crash is not a backoff signal
            assert job.error is not None and "stale claim" in job.error  # visible why
            reclaimed = await worker.claim_frame_share_one(db)  # the delivery can actually resume
            assert reclaimed is not None and reclaimed.id == job.id
            await db.rollback()
    finally:
        async with async_session() as db:
            if user_id is not None:
                await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tag).where(Tag.name == "curated"))
            await db.commit()


async def test_recover_leaves_a_fresh_claim_alone(adb, monkeypatch, tmp_path):
    # A worker that claimed a second ago is mid-send, not dead: re-queueing it would duplicate a
    # delivery that is still in flight. Only the horizon separates the two cases.
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    job.claimed_at = datetime.now(UTC)
    await adb.flush()
    assert await fs.recover_stale_claimed(adb) == 0
    assert job.state is FrameShareState.claimed and job.attempts == 0
    assert job.claimed_at is not None


async def test_recover_treats_null_claimed_at_as_stale(adb, monkeypatch, tmp_path):
    # A row claimed before claimed_at existed (the jobs stranded in prod today): no lease to expire,
    # but it can only be an orphan — every claim path stamps the column now.
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    assert job.claimed_at is None
    assert await fs.recover_stale_claimed(adb) == 1
    assert job.state is FrameShareState.queued and job.attempts == 1


async def test_recover_fails_job_at_the_attempt_ceiling(adb, monkeypatch, tmp_path):
    # A job whose send reliably kills the worker must end VISIBLY failed at FRAME_MAX_ATTEMPTS, not
    # be re-queued into a crash loop forever (a re-edit still resurrects it via the enqueue path).
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    job.attempts = fs.FRAME_MAX_ATTEMPTS - 1
    job.claimed_at = datetime.now(UTC) - timedelta(minutes=settings.FRAME_CLAIM_STALE_MINUTES + 1)
    await adb.flush()
    assert await fs.recover_stale_claimed(adb) == 1
    assert job.state is FrameShareState.failed
    assert job.attempts == fs.FRAME_MAX_ATTEMPTS
    assert job.error is not None and "gave up" in job.error
    assert job.claimed_by is None and job.claimed_at is None


async def test_recover_honors_the_configured_horizon(adb, monkeypatch, tmp_path):
    # The horizon is a real knob, not a hardcoded constant: the same row is spared or recovered
    # depending on FRAME_CLAIM_STALE_MINUTES.
    job, *_ = await _make_claimed(adb, monkeypatch, tmp_path)
    job.claimed_at = datetime.now(UTC) - timedelta(minutes=30)
    await adb.flush()
    monkeypatch.setattr(settings, "FRAME_CLAIM_STALE_MINUTES", 60)
    assert await fs.recover_stale_claimed(adb) == 0
    assert job.state is FrameShareState.claimed
    monkeypatch.setattr(settings, "FRAME_CLAIM_STALE_MINUTES", 5)
    assert await fs.recover_stale_claimed(adb) == 1
    assert job.state is FrameShareState.queued


async def test_recover_safe_swallows_errors(monkeypatch):
    # Boundary guard: a DB failure inside recovery must not escape into run() — at startup it would
    # kill the worker before the loop, and on the reap tick it would halt media processing.
    async def boom():
        raise RuntimeError("db exploded during recovery")

    monkeypatch.setattr(fs, "recover_stale_claimed_committed", boom)
    assert await worker.run_frame_share_recover_safe() == 0


def test_worker_loop_wires_stale_claim_recovery():
    # Wiring pin (like the upload reaper's): recovery that nothing calls fixes nothing. It must run
    # at STARTUP (the deploy --force-recreate that caused the strand) and again inside the loop.
    import inspect

    head, sep, loop = inspect.getsource(worker.run).partition("while True:")
    assert sep, "worker.run no longer has the drain loop this pin anchors on"
    assert "run_frame_share_recover_safe()" in head  # startup sweep, before any drain
    assert "run_frame_share_recover_safe()" in loop  # periodic, on the reap cadence
    # ...and through the COMMITTING entry — a flushed re-queue dies with the session (MEMNG-37).
    wrapper = inspect.getsource(worker.run_frame_share_recover_safe)
    assert "recover_stale_claimed_committed()" in wrapper


def test_rerender_is_byte_identical_so_the_album_sha_dedups_a_retry(tmp_path):
    # Underpins the duplicate-delivery trade documented in recover_stale_claimed: a recovered job
    # re-renders the same original with the same fixed parameters, so the bytes — and the sha256
    # aura-frames dedups on (200 = known photo) — match the first attempt's.
    p = tmp_path / "o.jpg"
    _write_jpeg(p, (300, 200))
    dt = datetime(2024, 3, 1, 12, 30, 0, tzinfo=UTC)
    assert fs.render_for_frame(str(p), dt) == fs.render_for_frame(str(p), dt)


async def test_unconfigured_worker_leaves_frame_jobs_queued(monkeypatch, tmp_path, db_engine):
    # The Merkur slow-lane worker runs the same image without the album API configured. It must NOT
    # claim frame-share jobs (it couldn't upload) — it leaves them queued for the Saturn worker.
    _frame_api_settings(monkeypatch, url="", album=0, token="")
    f = tmp_path / "real.jpg"
    _write_jpeg(f, (100, 70))
    subject = f"guard-{secrets.token_hex(4)}"
    user_id = None
    try:
        async with async_session() as db:
            u = User(
                subject=subject, role="user",
                frame_send_to="frame@send.auraframes.com", frame_send_from="me@me.com",
            )
            db.add(u)
            await db.flush()
            user_id = u.id
            att = Attachment(
                uid=secrets.token_urlsafe(8), owner_id=u.id, filename="p.jpg", mime="image/jpeg",
                size=1, status=AttachmentStatus.ready, storage_path=str(f),
            )
            db.add(att)
            await db.flush()
            memo = await memos_svc.create_memo(
                db, u, content="#curated", visibility=Visibility.PRIVATE, attachment_uids=[att.uid]
            )
            db.add(FrameShareJob(attachment_id=att.id, memo_id=memo.id, user_id=u.id,
                                 state=FrameShareState.queued, attempts=0))
            await db.commit()

        async with async_session() as db:
            assert await worker.run_frame_share_once(db) == "idle"

        async with async_session() as db:
            job = (
                await db.execute(select(FrameShareJob).where(FrameShareJob.user_id == user_id))
            ).scalar_one()
            assert job.state is FrameShareState.queued  # NOT claimed, NOT skipped
    finally:
        async with async_session() as db:
            if user_id is not None:
                await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tag).where(Tag.name == "curated"))
            await db.commit()
