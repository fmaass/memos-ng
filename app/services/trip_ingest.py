"""Home's puller for trip passes (plan PLAN_2026-10-27_trip-pass-pluto P3): pushes pass state and
device revocations to the ingest host and files the guest batches it has received into the trip.

The ingest host is LESS trusted than home. Everything it says is input: every manifest field is
validated before use, owner, collection, visibility and tag come only from home's own pass row,
and a file's bytes are taken only once their sha256 equals the manifest's. A manifest that fails
validation is quarantined on the ingest host and never acknowledged; a transport failure (link
down, timeout, 5xx) is retried on the next tick and never quarantines anything.

Home's `ingest_batches` / `ingest_blobs` rows are the import ledger, keyed by the ingest host's
batch id and judged by `state`, never by `memo_id` (which a deleted memo nulls):

* `open`        — recorded from the feed, not filed yet;
* `complete`    — filed: the memo, its attachments, their media jobs and this state committed in
                  ONE transaction; the ack is owed while `acked_at` is NULL;
* `acked`       — the ingest host confirmed the ack;
* `quarantined` — refused at home; the quarantine is owed while `quarantined_at` is NULL.

A batch is filed only from `open`, decided under its row lock BEFORE anything is inserted, so a
re-pull, a replay after a crash, or a memo the owner deleted never files it twice: the most a
second look does is retry the ack. Each feed cursor moves in the transaction that recorded its
page, so a crash re-reads a page and never skips one.

Revocations follow the plan's decision (the owner moderates): a batch the ingest host already
received is filed even when its pass or device was revoked later. A home revocation is sticky — a
pulled device never clears it — and is pushed every tick until the ingest host reports it applied.
"""

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import quote

import httpx
import structlog
from anyio import to_thread
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session, engine
from app.ingest import core as ingest_core
from app.models import (
    PASS_DEVICE_PLATFORMS,
    IngestBatch,
    IngestBlob,
    Pass,
    PassDevice,
    PassEvent,
    PassSyncState,
    TripIngestCursor,
)
from app.services import collections as coll_svc
from app.services import passes as passes_svc
from app.services import storage, variants
from app.services import uploads as uploads_svc

logger = structlog.get_logger(__name__)

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_BATCH_FILES = 2000
MAX_BATCH_BYTES = 50 * 1024**3
CAPTION_MAX = 2000
# The guest door's own bound (`guest.py` UploadIn.filename, and every filename column): a name
# the ingest host accepted is one home can file — placement sanitizes and byte-caps it.
FILENAME_MAX = 512
DISPLAY_NAME_MAX = 64
# The mirror's count columns (`pass_devices.uploads_count` INTEGER, `bytes_total` BIGINT): a value
# past them fails only inside PostgreSQL, aborting the whole page's transaction.
INT32_MAX = 2**31 - 1
INT64_MAX = 2**63 - 1
# Every service-API answer is read into memory up to this many bytes and no further. A page of
# BATCH_PAGE batches at the manifest caps (2000 files, 512-character names) stays well inside it.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BATCH_PAGE = 10
# A batch is filed in parts of at most this many files and bytes, one transaction each, so one
# part's filing fits a tick's bound whatever the batch's size (a single larger file is one part).
# A screen and its prints bind to each other only inside one finalize, so a variant group stays
# in one part while it fits one. A group too large for any part is cut across parts: its prints
# filed in a later part than their screen are promoted to visible pictures of that part's memo
# (finalize is passed no memo, so the memo-sibling bind cannot reach the earlier part's screen).
# The bound wins over the binding, because a part past it never completes and starves the queue.
FILE_PART_FILES = 25
FILE_PART_BYTES = 256 * 1024 * 1024
DEVICE_PAGE = 500
PUSH_PAGE = 500
REVOCATION_PAGE = 1000
CHUNK = 1024 * 1024
ERROR_TEXT_LIMIT = 500
# One tick at a time across every worker process: a transaction-scoped advisory lock held by the
# tick's own connection, so a crashed worker can never leave it behind.
SYNC_LOCK_KEY = 0x7472_7075  # "trpu"

_DATETIME = TypeAdapter(datetime)


class LinkDown(Exception):
    """The ingest host did not answer usefully: unreachable, timed out, 5xx, refused the token, or
    sent an answer that does not parse. Retryable — it never quarantines a batch."""


class Hostile(Exception):
    """A manifest (or the bytes it names) home will not take. Quarantined, never acknowledged."""


class Held(Exception):
    """A valid batch home cannot file right now (its trip has no usable tag, its pass or owner is
    gone). It stays `open` and is tried again every tick; nothing is invented to make it fit."""


class OutOfTime(Exception):
    """The tick's budget ran out between two pieces of work; what is done so far is kept."""


# --- configuration ------------------------------------------------------------------------------


def enabled() -> bool:
    return bool(settings.TRIP_INGEST_URL and settings.TRIP_INGEST_TOKEN_FILE)


def read_token() -> str:
    """The service token from its file, trailing newline stripped. Never logged."""
    token = Path(settings.TRIP_INGEST_TOKEN_FILE).read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("TRIP_INGEST_TOKEN_FILE is empty")
    return token


def make_client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.TRIP_INGEST_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=settings.TRIP_INGEST_TIMEOUT_S,
    )


def idempotency_key(batch_uid: str) -> str:
    """The memo's key for a batch: bounded (48 characters, `memos.idempotency_key` holds 64) and
    namespaced, so no member's client key can collide with it."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(batch_uid.encode()).digest()).rstrip(b"=")
    return f"trip:{digest.decode()}"


def staging_dir(batch_uid: str) -> Path:
    """Where a batch's files are downloaded before filing. Under `DATA_ROOT/uploads`, named only
    from a validated batch id and validated hashes — nothing the ingest host says names a path."""
    return Path(settings.DATA_ROOT) / "uploads" / "trip-ingest" / batch_uid


# --- transport ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    status_code: int
    body: bytes
    what: str  # "<METHOD> <path>", home's own words for the request


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Answer:
    """One service-API request, its body read only for a 200 and only up to MAX_RESPONSE_BYTES of
    raw (identity-encoded) bytes: the ingest host is less trusted, and neither a huge nor a
    compressed body may grow in memory before it is judged."""
    what = f"{method} {url}"
    headers = {**kwargs.pop("headers", {}), "Accept-Encoding": "identity"}
    try:
        async with client.stream(method, url, headers=headers, **kwargs) as r:
            status = r.status_code
            if status >= 500 or status in (401, 403, 408, 429):
                raise LinkDown(f"{what}: HTTP {status}")
            if status != 200:
                return Answer(status, b"", what)  # the body of a refusal is never read
            if r.headers.get("content-encoding", "identity").lower() != "identity":
                raise LinkDown(f"{what}: an encoded body")
            body = bytearray()
            async for chunk in r.aiter_raw():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise LinkDown(f"{what}: the body passes {MAX_RESPONSE_BYTES} bytes")
    except httpx.HTTPError as exc:
        raise LinkDown(f"{what}: {type(exc).__name__}") from None
    return Answer(status, bytes(body), what)


def _json(r: Answer) -> Any:
    try:
        return json.loads(r.body)
    except ValueError:
        raise LinkDown(f"{r.what}: not JSON") from None


def _batch_path(uid: str, verb: str) -> str:
    return f"/ingest/batches/{quote(uid, safe='')}/{verb}"


# --- validation ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobIn:
    sha256: str
    size: int
    mime: str
    filename: str
    last_modified: datetime | None


@dataclass(frozen=True)
class BatchIn:
    uid: str
    pass_uid: str
    device_id: str
    caption: str | None
    completed_at: datetime | None
    blobs: tuple[BlobIn, ...]


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _a_cursor(value: object) -> TypeGuard[int]:
    """A feed's `next`: it is committed into a BIGINT cursor column, where a larger value would
    abort the page's transaction on every tick."""
    return _is_int(value) and 0 <= value <= INT64_MAX


def _an_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _ids_of(entries: list, field: str) -> set[str]:
    return {e[field] for e in entries if isinstance(e, dict) and _an_id(e.get(field))}


def _datetime_or_none(value: object, what: str) -> datetime | None:
    """A timestamp a `timestamptz` column and the millisecond arithmetic downstream can hold: a
    value whose instant lies past year 9999 in UTC parses, then fails at the INSERT (aborting the
    page) or in the filing's conversion (on every tick)."""
    if value is None:
        return None
    try:
        parsed = _DATETIME.validate_python(value)
        if parsed.tzinfo is None:
            # A zoneless value is read as UTC, as `_epoch_ms` reads one, never as this host's
            # local time: `astimezone` on a naive year-1 value fails (year 0) in any zone.
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValidationError, ValueError, OverflowError):
        raise Hostile(f"{what} is not a timestamp") from None


def _text_ok(value: str) -> bool:
    """Text PostgreSQL will store: no NUL, and no lone surrogate (JSON can escape one)."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _filename_ok(name: object) -> bool:
    # A plain base name: the name reaches `place_master`, which sanitizes it, but home does not
    # rely on that for a value the ingest host chose.
    return (
        isinstance(name, str)
        and 0 < len(name) <= FILENAME_MAX
        and not name.startswith(".")
        and not any(c in name for c in "/\\\x00")
        and all(c.isprintable() for c in name)
    )


def validate_blob(raw: object) -> BlobIn:
    if not isinstance(raw, dict):
        raise Hostile("a file entry is not an object")
    sha, size, mime, name = raw.get("sha256"), raw.get("size"), raw.get("mime"), raw.get("filename")
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise Hostile("a file's sha256 is not 64 lowercase hex digits")
    if not _is_int(size) or not 0 < size <= settings.MAX_UPLOAD_MIB * 1024 * 1024:
        # Fixed words, never the value: the ingest host holds home's bearer and could reflect it.
        raise Hostile(f"{sha}: the declared size is outside 1..MAX_UPLOAD_MIB")
    if not _filename_ok(name):
        raise Hostile(f"{sha}: the file name is not a plain name")
    assert isinstance(name, str)
    # The ingest host's own allowlist: one extension -> MIME map for both sides, so an honest
    # manifest always agrees with it and a disagreement is the manifest's.
    ext = ingest_core.extension_of(name)
    if ext is None or not isinstance(mime, str) or mime != ingest_core.ALLOWED_EXTENSIONS[ext][1]:
        raise Hostile(f"{sha}: extension and MIME do not agree")
    return BlobIn(
        sha256=sha,
        size=size,
        mime=mime,
        filename=name,
        last_modified=_datetime_or_none(raw.get("last_modified"), "last_modified"),
    )


def validate_batch(raw: dict) -> BatchIn:
    """Every field of one feed entry, checked; `batch_id` was checked by the caller."""
    uid = raw["batch_id"]
    pass_uid, device_id, caption = raw.get("pass_uid"), raw.get("device_id"), raw.get("caption")
    if not isinstance(pass_uid, str) or not ID.fullmatch(pass_uid):
        raise Hostile("the pass id is malformed")
    if not isinstance(device_id, str) or not ID.fullmatch(device_id):
        raise Hostile("the device id is malformed or missing")
    if caption is not None and (
        not isinstance(caption, str) or len(caption) > CAPTION_MAX or not _text_ok(caption)
    ):
        raise Hostile("the caption is not text of at most 2000 characters")
    blobs_raw = raw.get("blobs")
    if not isinstance(blobs_raw, list) or not 0 < len(blobs_raw) <= MAX_BATCH_FILES:
        raise Hostile("the batch holds no files or more than 2000")
    blobs = tuple(validate_blob(b) for b in blobs_raw)
    if len({b.sha256 for b in blobs}) != len(blobs):
        raise Hostile("the batch names one file twice")
    if sum(b.size for b in blobs) > MAX_BATCH_BYTES:
        raise Hostile("the batch is larger than 50 GB")
    return BatchIn(
        uid=uid,
        pass_uid=pass_uid,
        device_id=device_id,
        caption=caption or None,
        completed_at=_datetime_or_none(raw.get("completed_at"), "completed_at"),
        blobs=blobs,
    )


# --- ledger helpers -----------------------------------------------------------------------------


async def _cursor(db: AsyncSession) -> TripIngestCursor:
    await db.execute(
        pg_insert(TripIngestCursor).values(id=1).on_conflict_do_nothing(index_elements=["id"])
    )
    row = await db.get(TripIngestCursor, 1, populate_existing=True)
    assert row is not None
    return row


async def _home_pass(db: AsyncSession, uid: str) -> Pass | None:
    return (await db.execute(select(Pass).where(Pass.uid == uid))).scalar_one_or_none()


async def _lock_passes(db: AsyncSession, uids: set[str]) -> None:
    """FOR UPDATE on every pass a transaction is about to write devices of, ascending by id in ONE
    statement, before any device row. That is the order every owner path takes (`_lock_pass`, then
    its devices; a collection delete's passes ascending), so a transaction holding several device
    rows cannot cycle with a device revoke or a collection delete."""
    if uids:
        await db.execute(
            select(Pass.id).where(Pass.uid.in_(sorted(uids))).order_by(Pass.id).with_for_update()
        )


async def _filed_bytes(db: AsyncSession, pass_id: int) -> int:
    total = (
        await db.execute(
            select(func.coalesce(func.sum(IngestBatch.byte_total), 0)).where(
                IngestBatch.pass_id == pass_id, IngestBatch.state != "quarantined"
            )
        )
    ).scalar_one()
    return int(total)


async def _note_pass_error(db: AsyncSession, pass_id: int, message: str) -> None:
    stmt = pg_insert(PassSyncState).values(pass_id=pass_id, last_error=message[:ERROR_TEXT_LIMIT])
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[PassSyncState.pass_id], set_={"last_error": stmt.excluded.last_error}
        )
    )


# --- step 1: push pass state --------------------------------------------------------------------


def _pass_state(p: Pass) -> dict[str, object]:
    return {
        "uid": p.uid,
        "secret_hash": p.secret_hash,
        "label": p.label,
        "scope": p.scope,
        "expires_at": p.expires_at.isoformat(),
        "max_devices": p.max_devices,
        "byte_cap": p.byte_cap,
        "version": p.version,
        "revoked_at": p.revoked_at.isoformat() if p.revoked_at else None,
        "revoked_version": p.revoked_version,
    }


async def push_passes(client: httpx.AsyncClient) -> int:
    """Push every pass whose version the ingest host has not acknowledged. Returns how many it
    acknowledged. A version moved by an edit racing this push stays owed: the ack is recorded as
    the version that was PUSHED, and the edit's own upsert re-raises the request after it."""
    async with async_session() as db:
        owed = list(
            (
                await db.execute(
                    select(Pass)
                    .outerjoin(PassSyncState, PassSyncState.pass_id == Pass.id)
                    .where(
                        (PassSyncState.pass_id.is_(None))
                        | (PassSyncState.acked_version < Pass.version)
                    )
                    .order_by(Pass.uid)
                    .limit(PUSH_PAGE)
                )
            ).scalars()
        )
        pushed = {p.uid: (p.id, p.version) for p in owed}
        body = {"passes": [_pass_state(p) for p in owed]}
    if not owed:
        return 0
    r = await _call(client, "PUT", "/ingest/passes", json=body)
    if r.status_code != 200:
        raise LinkDown(f"PUT /ingest/passes: HTTP {r.status_code}")
    answer = _json(r)
    if not isinstance(answer, list):
        raise LinkDown("PUT /ingest/passes: not a list")
    acked = 0
    async with async_session() as db:
        await _lock_passes(db, set(pushed))
        for item in answer:
            # The id is checked as text before the lookup hashes it (a list is unhashable).
            if (
                not isinstance(item, dict)
                or not _an_id(item.get("uid"))
                or item["uid"] not in pushed
            ):
                continue
            held = item.get("version")
            pass_id, version = pushed[item["uid"]]
            if not _is_int(held) or held < version:
                continue  # the ingest host holds less than was pushed: still owed
            # Never more than home pushed: a claim of a higher version than home has ever issued
            # must not silence the pushes still to come.
            stmt = pg_insert(PassSyncState).values(
                pass_id=pass_id, acked_version=version, last_push_at=func.now()
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[PassSyncState.pass_id],
                    set_={
                        "acked_version": func.greatest(PassSyncState.acked_version, version),
                        "last_push_at": func.now(),
                        "last_error": None,
                    },
                )
            )
            # Cleared only while the pass still stands at the version pushed; an edit committed
            # meanwhile keeps its own request (and its urgency) for the next tick.
            await db.execute(
                text(
                    "UPDATE pass_sync_state s SET push_urgent = false, push_requested_at = NULL "
                    "FROM passes p WHERE s.pass_id = p.id AND p.id = :pid AND p.version <= :v"
                ),
                {"pid": pass_id, "v": version},
            )
            acked += 1
        await db.commit()
    return acked


# --- step 2: push device revocations ------------------------------------------------------------


async def push_device_revocations(client: httpx.AsyncClient) -> int:
    """Push every home device revocation the ingest host has not confirmed, every tick until it
    does. Only records listed as `applied` are confirmed; an `unknown` one stays owed."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(PassDevice, Pass.uid)
                .join(Pass, Pass.id == PassDevice.pass_id)
                .where(
                    PassDevice.revoked_version.is_not(None),
                    (PassDevice.revocation_acked_version.is_(None))
                    | (PassDevice.revocation_acked_version < PassDevice.revoked_version),
                )
                .order_by(PassDevice.id)
                .limit(REVOCATION_PAGE)
            )
        ).all()
        owed = {d.uid: (d.id, pass_uid, d.revoked_version, d.revoked_at) for d, pass_uid in rows}
    if not owed:
        return 0
    body = {
        "revocations": [
            {
                "device_id": uid,
                "pass_uid": pass_uid,
                "version": version,
                "revoked_at": revoked_at.isoformat(),
            }
            for uid, (_, pass_uid, version, revoked_at) in owed.items()
        ]
    }
    r = await _call(client, "PUT", "/ingest/device-revocations", json=body)
    if r.status_code != 200:
        raise LinkDown(f"PUT /ingest/device-revocations: HTTP {r.status_code}")
    answer = _json(r)
    applied = answer.get("applied") if isinstance(answer, dict) else None
    if not isinstance(applied, list):
        raise LinkDown("PUT /ingest/device-revocations: no applied list")
    confirmed = 0
    async with async_session() as db:
        await _lock_passes(db, {o[1] for o in owed.values()})
        for record in applied:
            if not isinstance(record, dict):
                continue
            uid = record.get("device_id")
            if not _an_id(uid) or uid not in owed:
                continue
            device_id, pass_uid, version, _ = owed[uid]
            # The confirmation must name the record home sent: same pass, at least its version.
            if record.get("pass_uid") != pass_uid or not _is_int(record.get("version")):
                continue
            if record["version"] < version:
                continue
            device = await db.get(PassDevice, device_id, with_for_update=True)
            if device is not None and device.revoked_version is not None:
                device.revocation_acked_version = min(version, device.revoked_version)
                confirmed += 1
        await db.commit()
    unknown = answer.get("unknown") if isinstance(answer, dict) else None
    if unknown:
        logger.warning("trip_ingest.revocation_unknown", count=len(unknown))
    return confirmed


# --- step 3: pull devices into home's mirror ----------------------------------------------------


def _valid_device(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    uid, pass_uid, name = raw.get("device_id"), raw.get("pass_uid"), raw.get("display_name")
    if not (isinstance(uid, str) and ID.fullmatch(uid)):
        return None
    if not (isinstance(pass_uid, str) and ID.fullmatch(pass_uid)):
        return None
    if not (isinstance(name, str) and 0 < len(name) <= DISPLAY_NAME_MAX and _text_ok(name)):
        return None
    if raw.get("platform") not in PASS_DEVICE_PLATFORMS:
        return None
    counts = (raw.get("uploads_count"), raw.get("bytes_total"))
    if not all(
        _is_int(c) and 0 <= c <= top for c, top in zip(counts, (INT32_MAX, INT64_MAX), strict=True)
    ):
        return None
    try:
        first_seen = _datetime_or_none(raw.get("first_seen"), "first_seen")
        last_seen = _datetime_or_none(raw.get("last_seen"), "last_seen")
    except Hostile:
        return None
    if first_seen is None:
        return None
    return {
        "uid": uid,
        "pass_uid": pass_uid,
        "display_name": name,
        "platform": raw["platform"],
        "first_seen": first_seen,
        "last_seen": last_seen,
        "uploads_count": counts[0],
        "bytes_total": counts[1],
    }


async def _mirror_device(db: AsyncSession, d: dict) -> None:
    p = await _home_pass(db, d["pass_uid"])
    if p is None:
        return  # not a pass home issued: nothing to mirror it onto
    existing = (
        await db.execute(select(PassDevice).where(PassDevice.uid == d["uid"]).with_for_update())
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            PassDevice(
                uid=d["uid"],
                pass_id=p.id,
                display_name=d["display_name"],
                platform=d["platform"],
                first_seen=d["first_seen"],
                last_seen=d["last_seen"],
                uploads_count=d["uploads_count"],
                bytes_total=d["bytes_total"],
            )
        )
        return
    if existing.pass_id != p.id:
        # A device never changes pass; a feed entry that says so is not believed.
        logger.warning("trip_ingest.device_pass_mismatch", device=d["uid"])
        return
    # The ingest host's activity figures only. `revoked_*` are home's and a pull never touches
    # them, so a device the ingest host still shows live cannot clear a home revocation.
    existing.display_name = d["display_name"]
    existing.platform = d["platform"]
    existing.last_seen = d["last_seen"]
    existing.uploads_count = d["uploads_count"]
    existing.bytes_total = d["bytes_total"]


async def pull_devices(client: httpx.AsyncClient) -> int:
    seen = 0
    while True:
        async with async_session() as db:
            since = (await _cursor(db)).devices_since
            await db.commit()
        r = await _call(
            client, "GET", "/ingest/devices", params={"since": since, "limit": DEVICE_PAGE}
        )
        if r.status_code != 200:
            raise LinkDown(f"GET /ingest/devices: HTTP {r.status_code}")
        page = _json(r)
        devices = page.get("devices") if isinstance(page, dict) else None
        nxt = page.get("next") if isinstance(page, dict) else None
        if not isinstance(devices, list) or not _a_cursor(nxt) or nxt < since:
            raise LinkDown("GET /ingest/devices: malformed page")
        if not devices:
            return seen
        valid = [d for d in map(_valid_device, devices) if d is not None]
        if len(valid) < len(devices):
            logger.warning("trip_ingest.device_refused", count=len(devices) - len(valid))
        async with async_session() as db:
            await _lock_passes(db, {d["pass_uid"] for d in valid})
            for d in valid:
                await _mirror_device(db, d)
            (await _cursor(db)).devices_since = nxt
            await db.commit()
        seen += len(devices)
        if len(devices) < DEVICE_PAGE or nxt == since:
            return seen


# --- step 4: pull batch manifests into the ledger -----------------------------------------------


async def _push_quarantine(client: httpx.AsyncClient, uid: str) -> bool:
    """Tell the ingest host to hold a batch. True once it holds it (or has no such batch)."""
    r = await _call(client, "POST", _batch_path(uid, "quarantine"))
    if r.status_code in (200, 404):
        return True
    raise LinkDown(f"quarantine {uid}: HTTP {r.status_code}")


async def _record_batch(
    db: AsyncSession, client: httpx.AsyncClient, raw: object, errors: list[str]
) -> None:
    """Turn one feed entry into a ledger row. Already recorded → nothing: home's own state
    decides what happens next, whatever the feed says now."""
    uid = raw.get("batch_id") if isinstance(raw, dict) else None
    if not isinstance(uid, str) or not ID.fullmatch(uid):
        # Not addressable on the ingest host's routes; it can never be filed or acknowledged.
        errors.append("a batch id is malformed")
        logger.warning("trip_ingest.batch_unaddressable")
        return
    assert isinstance(raw, dict)
    known = (
        await db.execute(select(IngestBatch.id).where(IngestBatch.uid == uid))
    ).scalar_one_or_none()
    if known is not None:
        return
    state = raw.get("state")
    if state in ("acked", "quarantined"):
        return  # settled by home before (only home acks or quarantines)
    try:
        if state != "complete":
            raise Hostile("the batch's state is not complete")
        batch = validate_batch(raw)
        p = await _home_pass(db, batch.pass_uid)
        if p is None:
            raise Hostile("the pass is not one home issued")
    except Hostile as exc:
        p = None
        pass_uid = raw.get("pass_uid")
        if _an_id(pass_uid):
            p = await _home_pass(db, pass_uid)
        if p is None:
            # No home pass to hang a ledger row on: quarantined NOW, inside the transaction that
            # moves the cursor past it, so a link failure leaves the cursor where it was.
            await _push_quarantine(client, uid)
            logger.warning("trip_ingest.quarantined", batch=uid, reason=str(exc))
            errors.append(f"{uid}: {exc}")
            return
        await _record_refused(db, uid, p, str(exc))
        errors.append(f"{uid}: {exc}")
        return
    device = (
        await db.execute(select(PassDevice).where(PassDevice.uid == batch.device_id))
    ).scalar_one_or_none()
    reason = None
    if device is None:
        reason = "the device is unknown to home"
    elif device.pass_id != p.id:
        reason = "the device belongs to another pass"
    elif await _filed_bytes(db, p.id) + sum(b.size for b in batch.blobs) > p.byte_cap:
        reason = "the batch exceeds the pass's byte cap"
    if reason is not None:
        await _record_refused(db, uid, p, reason)
        errors.append(f"{uid}: {reason}")
        return
    assert device is not None
    row = IngestBatch(
        uid=uid,
        pass_id=p.id,
        pass_device_id=device.id,
        caption=batch.caption,
        state="open",
        file_count=len(batch.blobs),
        byte_total=sum(b.size for b in batch.blobs),
        completed_at=batch.completed_at,
    )
    db.add(row)
    await db.flush()
    db.add_all(
        IngestBlob(
            batch_id=row.id,
            sha256=b.sha256,
            size_bytes=b.size,
            mime=b.mime,
            filename=b.filename,
            last_modified=b.last_modified,
        )
        for b in batch.blobs
    )


async def _record_refused(db: AsyncSession, uid: str, p: Pass, reason: str) -> None:
    """A refused batch's ledger row: `quarantined`, its push owed. Its files are not recorded —
    they are exactly what home declined to believe."""
    db.add(IngestBatch(uid=uid, pass_id=p.id, state="quarantined"))
    db.add(PassEvent(pass_id=p.id, kind="refused", version=p.version, detail=f"{uid}: {reason}"))
    await _note_pass_error(db, p.id, f"batch {uid} quarantined: {reason}")
    logger.warning("trip_ingest.quarantined", batch=uid, reason=reason)


async def pull_batches(client: httpx.AsyncClient, errors: list[str]) -> int:
    seen = 0
    while True:
        async with async_session() as db:
            since = (await _cursor(db)).batches_since
            await db.commit()
        r = await _call(
            client, "GET", "/ingest/batches", params={"since": since, "limit": BATCH_PAGE}
        )
        if r.status_code != 200:
            raise LinkDown(f"GET /ingest/batches: HTTP {r.status_code}")
        page = _json(r)
        batches = page.get("batches") if isinstance(page, dict) else None
        nxt = page.get("next") if isinstance(page, dict) else None
        if not isinstance(batches, list) or not _a_cursor(nxt) or nxt < since:
            raise LinkDown("GET /ingest/batches: malformed page")
        if not batches:
            return seen
        # A batch whose device the mirror does not hold yet: the device feed is drained again
        # BEFORE this page's transaction, because the ingest host commits a batch and its device's
        # cursor together, so a drain started now serves every device this page names.
        # Only well-formed ids are hashed or sent to PostgreSQL: a list is unhashable and a NUL
        # is refused by the database, either one aborting the page before any entry is judged.
        # The entry itself is judged (and quarantined) by `_record_batch`.
        named = _ids_of(batches, "device_id")
        async with async_session() as db:
            mirrored = set(
                (await db.execute(select(PassDevice.uid).where(PassDevice.uid.in_(named))))
                .scalars()
                .all()
            )
        if named - mirrored:
            await pull_devices(client)
        async with async_session() as db:
            # A refusal writes its pass's sync row and event, so the page takes its passes first.
            await _lock_passes(db, _ids_of(batches, "pass_uid"))
            for raw in batches:
                await _record_batch(db, client, raw, errors)
            (await _cursor(db)).batches_since = nxt
            await db.commit()
        seen += len(batches)
        if len(batches) < BATCH_PAGE or nxt == since:
            return seen


# --- step 5: download ---------------------------------------------------------------------------


def _size_sync(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _append_sync(path: Path, data: bytes, truncate: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb" if truncate else "ab") as f:
        f.write(data)


def _hasher_sync(path: Path) -> "hashlib._Hash":
    """A running sha256 over what an earlier attempt left in `path`, for a resume to continue."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h


def _append_hashed_sync(path: Path, data: bytes, truncate: bool, h: "hashlib._Hash") -> None:
    _append_sync(path, data, truncate)
    h.update(data)


def _unlink_sync(path: Path) -> None:
    path.unlink(missing_ok=True)


def _rmtree_sync(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _verified_path(batch_uid: str, sha: str) -> Path:
    """A file that passed every check. Only a verified file carries the bare hash as its name, so
    its presence IS the durable record that it needs no download and no hashing again."""
    return staging_dir(batch_uid) / sha


def _partial_path(batch_uid: str, sha: str) -> Path:
    return staging_dir(batch_uid) / f"{sha}.part"


async def _staging_write(fn: Callable[..., object], *args: object) -> None:
    """A staging file write, append, truncate, unlink or rename, waited out to its end. A worker
    thread cannot be cancelled: unwound around it, the tick would release its advisory lock while
    the thread still writes, and the next tick would write the same partial file beside it. The
    cancellation is raised once the thread has returned."""
    _, cancelled = await uploads_svc.finish_off_the_loop(fn, *args)
    if cancelled is not None:
        raise cancelled


async def _fetch(client: httpx.AsyncClient, blob: IngestBlob, dest: Path, deadline: float) -> str:
    """Stream one file into `dest`, continuing from what an earlier attempt left there (Range),
    and answer the sha256 of the whole file, hashed as it arrives. The running count aborts the
    moment the bytes pass the declared size; a cut stream keeps what arrived for the next attempt.
    The body is read raw and must be identity-encoded: a decoder could hand over far more bytes
    in one piece than the declared size before any count saw them."""
    have = await to_thread.run_sync(_size_sync, dest)
    if have > blob.size_bytes:
        await _staging_write(_unlink_sync, dest)
        have = 0
    hasher = await to_thread.run_sync(_hasher_sync, dest) if have else hashlib.sha256()
    if have == blob.size_bytes:
        return hasher.hexdigest()  # every byte came from an earlier attempt
    headers = {"Accept-Encoding": "identity"}
    if have:
        headers["Range"] = f"bytes={have}-"
    url = f"/ingest/blobs/{blob.sha256}"
    try:
        async with client.stream("GET", url, headers=headers) as r:
            if r.status_code >= 500 or r.status_code in (401, 403, 408, 429):
                raise LinkDown(f"GET {url}: HTTP {r.status_code}")
            if r.status_code == 404:
                raise Hostile(f"{blob.sha256}: the ingest host has no such file")
            if r.status_code == 416:
                await _staging_write(_unlink_sync, dest)
                raise LinkDown(f"GET {url}: range refused, restarting")
            if r.status_code not in (200, 206):
                raise LinkDown(f"GET {url}: HTTP {r.status_code}")
            if r.headers.get("content-encoding", "identity").lower() != "identity":
                raise Hostile(f"{blob.sha256}: the file came encoded")
            resumed = r.status_code == 206 and have > 0
            # A 200 to a ranged request is the whole file again.
            offset = have if resumed else 0
            truncate = not resumed
            if not resumed:
                hasher = hashlib.sha256()
            # Unsized: a chunk size would buffer up to it, and a link dropping inside that buffer
            # would lose bytes that had arrived, which is what a resume is for.
            async for chunk in r.aiter_raw():
                if not chunk:
                    continue
                offset += len(chunk)
                if offset > blob.size_bytes:
                    await _staging_write(_unlink_sync, dest)
                    raise Hostile(f"{blob.sha256}: the file streams past its declared size")
                await _staging_write(_append_hashed_sync, dest, chunk, truncate, hasher)
                truncate = False
                if time.monotonic() >= deadline:
                    raise OutOfTime
            if truncate:  # an empty body to a whole-file request
                await _staging_write(_append_sync, dest, b"", True)
    except httpx.HTTPError as exc:
        raise LinkDown(f"GET {url}: {type(exc).__name__}") from None
    return hasher.hexdigest()


async def download(
    client: httpx.AsyncClient, batch_uid: str, blobs: Sequence[IngestBlob], deadline: float
) -> None:
    """Every file of a batch, staged and verified: complete, its sha256 the manifest's, its bytes
    the type its name claims. A file verified on an earlier tick is not read again, so a large
    batch makes progress across ticks instead of re-hashing its first files each time."""
    for blob in blobs:
        if blob.filed_at is not None:
            continue  # already in a memo
        verified = _verified_path(batch_uid, blob.sha256)
        if await to_thread.run_sync(_size_sync, verified) == blob.size_bytes:
            continue
        if time.monotonic() >= deadline:
            raise OutOfTime
        dest = _partial_path(batch_uid, blob.sha256)
        for attempt in (1, 2):
            have = await to_thread.run_sync(_size_sync, dest)
            digest = await _fetch(client, blob, dest, deadline)
            short = await to_thread.run_sync(_size_sync, dest) != blob.size_bytes
            if not short and digest == blob.sha256:
                break
            await _staging_write(_unlink_sync, dest)
            # `_fetch` returned, so the answer ended by its own framing (a cut link raises
            # LinkDown there): those are all the bytes the ingest host will send. A length or a
            # hash that disagrees with the manifest is then the manifest's fault, never the
            # link's; retried, a short one would be resumed into a 416 and restarted on every
            # tick, with every batch behind it stuck. One fresh download after a resumed one went
            # wrong, then the batch is quarantined.
            if not have or attempt == 2:
                if short:
                    raise Hostile(f"{blob.sha256}: the file ends before its declared size")
                raise Hostile(f"{blob.sha256}: the bytes do not hash to their name")
        if await to_thread.run_sync(ingest_core.sniff_matches, dest, blob.filename) != blob.mime:
            raise Hostile(f"{blob.sha256}: the bytes are not the type the name claims")
        await _staging_write(_replace_sync, dest, verified)


# --- step 6: file -------------------------------------------------------------------------------


async def _after_filing_commit() -> None:
    """A test seam: the instant after the filing commit and before the ack, where a crash must
    leave one memo and an ack still owed."""


def _replace_sync(src: Path, dest: Path) -> None:
    os.replace(src, dest)


def _restore(moved: list[storage.TrackedMove], parts: list[tuple[Path, Path]]) -> None:
    """Walk a rolled-back filing's files back to the staging area, so the next tick finds them.
    Inline, not in a thread: this may run while a cancellation unwinds (see the memo router's
    `_restore_moved`). A file that will not move is logged and left where it is."""
    for move in reversed(moved):
        unrestored = move.restore()
        if unrestored is not None:
            logger.error("trip_ingest.restore_failed", src=move.src, dest=unrestored)
    for staged, part in parts:
        try:
            if staged.exists():
                # The rename into the upload never ran (a cancellation or a failure before it):
                # the verified bytes are still staged and the part file is the empty placeholder
                # `create_upload` made, which must not be moved over them.
                part.unlink(missing_ok=True)
            elif part.exists():
                os.replace(part, staged)
        except OSError as exc:
            logger.error("trip_ingest.restore_failed", src=str(part), error=type(exc).__name__)


def _epoch_ms(value: datetime) -> int:
    """Milliseconds since 1970, in integer arithmetic: `timestamp() * 1000` failed on the last
    instants of year 9999 (the value lands in year 10000), on every tick. The driver hands such an
    instant back without its zone; a `timestamptz` value is a UTC instant either way."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - uploads_svc.EPOCH) // timedelta(milliseconds=1)


async def _filing_committed(batch_id: int, blob_ids: list[int]) -> bool | None:
    """Whether a filing whose COMMIT was interrupted committed after all; None when that cannot be
    learned. PostgreSQL may have committed before the interruption reached `commit()`, and then
    the memo's rows point at the placed originals. Taking the batch row's lock waits until the
    interrupted transaction has ended either way, so the filed marks read here are its outcome."""
    try:
        async with async_session() as db:
            await db.execute(text("SET LOCAL lock_timeout = '30s'"))
            await db.execute(
                select(IngestBatch.id).where(IngestBatch.id == batch_id).with_for_update()
            )
            filed = (
                await db.execute(
                    select(func.count())
                    .select_from(IngestBlob)
                    .where(IngestBlob.id.in_(blob_ids), IngestBlob.filed_at.is_not(None))
                )
            ).scalar_one()
            await db.rollback()
            return filed > 0
    except Exception:
        logger.exception("trip_ingest.commit_outcome_unknown", batch_id=batch_id)
        return None


async def _filing_target(db: AsyncSession, batch: IngestBatch) -> tuple[Pass, Any]:
    p = await db.get(Pass, batch.pass_id)
    if p is None or p.created_by is None:
        raise Held("the pass or its issuer is gone")
    collection = await passes_svc.pass_collection(db, p)
    if collection is None:
        raise Held("the pass's collection is gone")
    try:
        # The trip's tag as it stands NOW: an edit since issuance may have changed or removed it.
        passes_svc.guest_memo_content(collection)
    except passes_svc.TripHasNoTag:
        raise Held("the trip has no tag a guest memo can carry") from None
    return p, collection


def _filing_parts(blobs: Sequence[IngestBlob]) -> list[list[IngestBlob]]:
    """A batch's unfiled files cut into filing parts: no part holds more than FILE_PART_FILES
    files or FILE_PART_BYTES bytes, except a part of exactly one larger file. Files are grouped
    by variant base (a screen and its prints), groups in the order they first appear and each
    group's files in manifest order. A group that fits a part goes into one part whole; a group
    that does not fit even on its own is cut greedily, file by file, into bounded parts."""
    groups: dict[str, list[IngestBlob]] = {}
    for blob in blobs:
        groups.setdefault(variants.parse_variant(blob.filename).base.casefold(), []).append(blob)
    parts: list[list[IngestBlob]] = []
    current: list[IngestBlob] = []
    size = 0

    def add(files: list[IngestBlob]) -> None:
        nonlocal current, size
        grown = sum(b.size_bytes for b in files)
        if current and (
            len(current) + len(files) > FILE_PART_FILES or size + grown > FILE_PART_BYTES
        ):
            parts.append(current)
            current, size = [], 0
        current += files
        size += grown

    for group in groups.values():
        whole = len(group) == 1 or (
            len(group) <= FILE_PART_FILES and sum(b.size_bytes for b in group) <= FILE_PART_BYTES
        )
        if whole:
            add(group)
        else:
            # Kept whole, this group would be one part past the tick's bound, cancelled and
            # retried forever with the batch behind it never filed (the gate's finding 3).
            for blob in group:
                add([blob])
    if current:
        parts.append(current)
    return parts


async def _file_part(batch_id: int) -> bool | None:
    """File the next part of a downloaded batch as one guest memo, in one transaction with the
    ledger marks that say it is filed. None: the batch is not `open` (filed, quarantined — the
    replay is resolved here, before anything is inserted); True: this part completed the batch;
    False: parts remain for the next call."""
    moved: list[storage.TrackedMove] = []
    parts: list[tuple[Path, Path]] = []
    consumed: list[str] = []
    filing: list[int] = []
    committed = commit_sent = False
    try:
        async with async_session() as db:
            try:
                batch = (
                    await db.execute(
                        select(IngestBatch)
                        .where(IngestBatch.id == batch_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if batch is None or batch.state != "open":
                    return None
                uid = batch.uid
                p, collection = await _filing_target(db, batch)
                assert p.created_by is not None
                owner_id = p.created_by
                blobs = list(
                    (
                        await db.execute(
                            select(IngestBlob)
                            .where(IngestBlob.batch_id == batch.id)
                            .order_by(IngestBlob.id)
                        )
                    ).scalars()
                )
                pending = [b for b in blobs if b.filed_at is None]
                part = _filing_parts(pending)[0] if pending else []
                # The first part keeps the batch's own key, so a batch of one part is keyed
                # exactly as a whole batch; a later part is keyed by the file it starts with.
                key = idempotency_key(
                    uid if len(pending) == len(blobs) else f"{uid}/{part[0].sha256}"
                )
                upload_for: dict[str, IngestBlob] = {}
                for blob in part:
                    up = await uploads_svc.create_upload(
                        db,
                        owner_id=owner_id,
                        filename=blob.filename,
                        mime=blob.mime,
                        size=blob.size_bytes,
                        last_modified=(
                            _epoch_ms(blob.last_modified) if blob.last_modified else None
                        ),
                    )
                    staged = _verified_path(uid, blob.sha256)
                    target = uploads_svc.part_path(up.upload_uid)
                    parts.append((staged, target))
                    await to_thread.run_sync(_replace_sync, staged, target)
                    # The completion state a member's last chunk leaves: every byte received, its
                    # hash known. The capture read happens in finalize's `ensure_capture`.
                    up.bytes_received = blob.size_bytes
                    up.sha256 = blob.sha256
                    upload_for[up.upload_uid] = blob
                await db.flush()
                device = (
                    await db.get(PassDevice, batch.pass_device_id)
                    if batch.pass_device_id is not None
                    else None
                )
                resolved: dict[str, str] = {}
                positions: dict[str, int] = {}
                order = list(upload_for)

                async def finalize(memo) -> list[str]:
                    done = await uploads_svc.finalize_pending(
                        db,
                        order,
                        owner_id,
                        moved_out=moved,
                        consumed_out=consumed,
                        positions_out=positions,
                        resolved_out=resolved,
                        # Fresh rows always: a guest's bytes never take over an attachment the owner
                        # already has, its renditions or its placement (the gate's finding 4).
                        adopt_unlinked=False,
                        # Hashed by `download` from these very files, which only home has touched.
                        known_digests={u: b.sha256 for u, b in upload_for.items()},
                    )
                    return sorted(done, key=lambda u: positions.get(u, len(done)))

                memo = await passes_svc.create_guest_memo(
                    db,
                    p,
                    caption=batch.caption,
                    contributor_name=device.display_name if device is not None else None,
                    idempotency_key=key,
                    device=device,
                    finalize=finalize,
                    moved_out=moved,
                )
                by_uid = {a.uid: a for a in memo.attachments}
                now = datetime.now(UTC)
                for up_uid, blob in upload_for.items():
                    att = by_uid.get(resolved.get(up_uid, ""))
                    blob.attachment_id = att.id if att is not None else None
                    blob.filed_at = now
                if collection.kind == "album":
                    # An album's membership is its member rows, not a tag (`guest_memo_content`).
                    for att in sorted(memo.attachments, key=lambda a: a.id):
                        await coll_svc.add_member(db, collection.id, att.id, added_by=owner_id)
                if batch.memo_id is None:
                    batch.memo_id = memo.id
                complete = len(part) == len(pending)
                if complete:
                    batch.state = "complete"
                filing = [b.id for b in part]
                commit_sent = True
                await db.commit()
                committed = True
            except BaseException:
                if not commit_sent:
                    # blocking-ok: a cancellation may be unwinding through here, and an await
                    # could be interrupted before the files are back (the memo router restores
                    # inline too).
                    _restore(moved, parts)
                raise
    except BaseException:
        if commit_sent and not committed:
            # Interrupted inside COMMIT: undoing the placement is right only if PostgreSQL did not
            # commit, so the outcome is read back first (its session has closed by now, so the
            # interrupted transaction is over or ending). Shielded, because a cancellation is
            # what usually lands here; interrupted again or unreadable, the files stay where they
            # are: committed, they are where the rows say; not, the batch is still open and
            # unacked, so the next tick downloads them again.
            outcome = asyncio.ensure_future(_filing_committed(batch_id, filing))
            try:
                committed_after_all = await asyncio.shield(outcome)
            except BaseException:
                committed_after_all = None
            if committed_after_all is False:
                _restore(moved, parts)  # blocking-ok: as above
        raise
    if consumed:
        await to_thread.run_sync(uploads_svc.reap_consumed_sync, consumed)
    if complete:
        await to_thread.run_sync(_rmtree_sync, staging_dir(uid))
    logger.info("trip_ingest.filed", batch=uid, memo=memo.uid, files=len(part), complete=complete)
    return complete


async def file_batch(batch_id: int, deadline: float = math.inf) -> bool:
    """File a downloaded batch, part after part, each part durable on its own. True when THIS
    call completed the batch; False when it was not `open` any more. Out of time between parts:
    OutOfTime, and the next tick continues after the last committed part."""
    while True:
        done = await _file_part(batch_id)
        if done is None:
            return False
        if done:
            return True
        if time.monotonic() >= deadline:
            raise OutOfTime


# --- step 7/8: settle with the ingest host ------------------------------------------------------


async def _ack(client: httpx.AsyncClient, batch_id: int, uid: str) -> None:
    r = await _call(client, "POST", _batch_path(uid, "ack"))
    if r.status_code not in (200, 404):
        # A 409 is a filed batch the ingest host has quarantined; nothing home does changes it.
        logger.warning("trip_ingest.ack_refused", batch=uid, status=r.status_code)
        return
    async with async_session() as db:
        batch = await db.get(IngestBatch, batch_id, with_for_update=True)
        if batch is not None and batch.state == "complete":
            batch.state = "acked"
            batch.acked_at = datetime.now(UTC)
        await db.commit()


async def _quarantine(client: httpx.AsyncClient, batch_id: int, uid: str) -> None:
    await _push_quarantine(client, uid)
    async with async_session() as db:
        batch = await db.get(IngestBatch, batch_id, with_for_update=True)
        if batch is not None and batch.state == "quarantined" and batch.quarantined_at is None:
            batch.quarantined_at = datetime.now(UTC)
        await db.commit()


async def settle(client: httpx.AsyncClient) -> None:
    """Deliver every owed ack and quarantine. A quarantined batch is never acknowledged: the two
    sets are disjoint by state."""
    async with async_session() as db:
        owed = (
            await db.execute(
                select(IngestBatch.id, IngestBatch.uid, IngestBatch.state)
                .where(
                    ((IngestBatch.state == "complete") & IngestBatch.acked_at.is_(None))
                    | ((IngestBatch.state == "quarantined") & IngestBatch.quarantined_at.is_(None))
                )
                .order_by(IngestBatch.id)
            )
        ).all()
    for batch_id, uid, state in owed:
        if state == "complete":
            await _ack(client, batch_id, uid)
        else:
            await _quarantine(client, batch_id, uid)


async def _refuse_open(batch_id: int, reason: str) -> None:
    """An `open` batch whose bytes failed: quarantined, its staged files dropped."""
    async with async_session() as db:
        batch = await db.get(IngestBatch, batch_id, with_for_update=True)
        if batch is None or batch.state != "open":
            return
        p = await db.get(Pass, batch.pass_id)
        batch.state = "quarantined"
        if p is not None:
            db.add(
                PassEvent(
                    pass_id=p.id, kind="refused", version=p.version, detail=f"{batch.uid}: {reason}"
                )
            )
            await _note_pass_error(db, p.id, f"batch {batch.uid} quarantined: {reason}")
        uid = batch.uid
        await db.commit()
    await to_thread.run_sync(_rmtree_sync, staging_dir(uid))
    logger.warning("trip_ingest.quarantined", batch=uid, reason=reason)


async def file_open(client: httpx.AsyncClient, deadline: float, errors: list[str]) -> int:
    filed = 0
    async with async_session() as db:
        ids = (
            await db.execute(
                select(IngestBatch.id).where(IngestBatch.state == "open").order_by(IngestBatch.id)
            )
        ).scalars()
        ids = list(ids)
    for batch_id in ids:
        if time.monotonic() >= deadline:
            raise OutOfTime
        async with async_session() as db:
            batch = await db.get(IngestBatch, batch_id)
            if batch is None or batch.state != "open":
                continue
            uid, pass_id = batch.uid, batch.pass_id
            try:
                await _filing_target(db, batch)  # cheap, so a held batch downloads nothing
            except Held as exc:
                await _note_pass_error(db, batch.pass_id, f"batch {uid} held: {exc}")
                await db.commit()
                errors.append(f"{uid} held: {exc}")
                logger.warning("trip_ingest.held", batch=uid, reason=str(exc))
                continue
            blobs = list(
                (
                    await db.execute(
                        select(IngestBlob)
                        .where(IngestBlob.batch_id == batch_id)
                        .order_by(IngestBlob.id)
                    )
                ).scalars()
            )
        try:
            await download(client, uid, blobs, deadline)
            if not await file_batch(batch_id, deadline):
                continue  # not open any more: whatever it owes, settle() delivers
        except Hostile as exc:
            await _refuse_open(batch_id, str(exc))
            errors.append(f"{uid}: {exc}")
            continue
        except Held as exc:
            errors.append(f"{uid} held: {exc}")
            continue
        except (LinkDown, OutOfTime):
            raise  # the link or the tick, not this batch: the tick ends where it stands
        except Exception as exc:
            # This batch's own failure (a file system refusal, say) stays this batch's: it is
            # retried next tick, and the batches behind it still get their turn.
            logger.exception("trip_ingest.filing_failed", batch=uid)
            async with async_session() as db:
                await _note_pass_error(db, pass_id, f"batch {uid}: {type(exc).__name__}")
                await db.commit()
            errors.append(f"{uid}: {type(exc).__name__}")
            continue
        filed += 1
        await _after_filing_commit()
        await _ack(client, batch_id, uid)
    return filed


# --- the tick -----------------------------------------------------------------------------------


@dataclass
class TickResult:
    status: str
    filed: int = 0
    errors: tuple[str, ...] = ()


async def sync_once(client: httpx.AsyncClient, budget_s: float) -> TickResult:
    """One tick, in order: push pass state, push device revocations, pull devices, pull
    manifests, settle what earlier ticks left owed, file, settle again."""
    deadline = time.monotonic() + budget_s
    errors: list[str] = []
    await push_passes(client)
    await push_device_revocations(client)
    await pull_devices(client)
    await pull_batches(client, errors)
    await settle(client)
    try:
        filed = await file_open(client, deadline, errors)
    except OutOfTime:
        # Out of budget, not out of order: what earlier filings owe is still delivered. Any other
        # failure ends the tick where it stands, and the next tick's settle picks it up.
        await settle(client)
        return TickResult("partial", 0, tuple(errors))
    await settle(client)
    return TickResult("ok", filed, tuple(errors))


async def _record_tick(*, synced: bool, error: str | None) -> None:
    async with async_session() as db:
        cur = await _cursor(db)
        if synced:
            cur.last_sync_at = datetime.now(UTC)
        cur.last_error = error[:ERROR_TEXT_LIMIT] if error else None
        await db.commit()


async def run_sync_safe(client: httpx.AsyncClient | None = None, budget_s: float = 10.0) -> str:
    """The worker's boundary guard around one tick: one tick at a time (advisory lock), every
    error isolated and recorded, so a failing tick leaves `last_error` set and `last_sync_at`
    unchanged."""
    if client is None and not enabled():
        return "disabled"
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                got = (
                    await conn.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SYNC_LOCK_KEY}
                    )
                ).scalar_one()
                if not got:
                    return "busy"
                own = client is None
                if client is None:
                    http = make_client(await to_thread.run_sync(read_token))
                else:
                    http = client
                try:
                    result = await sync_once(http, budget_s)
                finally:
                    if own:
                        await http.aclose()
                await _record_tick(synced=True, error="; ".join(result.errors) or None)
                return result.status
    except LinkDown as exc:
        logger.warning("trip_ingest.link_down", error=str(exc))
        await _record_tick_safe(f"link: {exc}")
        return "link_down"
    except Exception as exc:
        logger.exception("trip_ingest.error")
        await _record_tick_safe(f"{type(exc).__name__}: {exc}")
        return "error"


async def _record_tick_safe(error: str) -> None:
    try:
        await _record_tick(synced=False, error=error)
    except Exception:
        logger.exception("trip_ingest.record_failed")
