"""Shared pieces of the two ingest roles: storage layout, pass state, hashing, cursors, sniffing."""

import hashlib
import hmac
import shutil
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import INGEST_CHANGE_SEQ, Pass, PassDevice
from app.services.uploads import detect_heif_mime
from app.services.uploads import finish_off_the_loop as finish_off_the_loop  # re-export


def ingest_root() -> Path:
    return Path(settings.INGEST_DATA_ROOT)


def part_path(upload_uid: str) -> Path:
    return ingest_root() / "parts" / f"{upload_uid}.part"


def blob_relpath(sha256: str) -> str:
    """Content-addressed: one physical copy per hash, whichever batches name it, so the download by
    hash is deterministic and a purge can tell whether another batch still needs the bytes."""
    return f"blobs/{sha256[:2]}/{sha256}"


def blob_path(sha256: str) -> Path:
    return ingest_root() / blob_relpath(sha256)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def same_secret(presented_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(presented_hash.encode(), stored_hash.encode())


def client_ip(request: Request) -> str:
    """The client's address. `CF-Connecting-IP` is believed only where the deploy makes cloudflared
    the one peer that can reach the process (the `ingest` role, opted in by setting)."""
    if settings.INGEST_TRUST_CF_CONNECTING_IP:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client is not None else "unknown"


def ip_hash(ip: str) -> str:
    return hashlib.sha256(f"{settings.INGEST_IP_HASH_SALT}|{ip}".encode()).hexdigest()


PassState = Literal["active", "expired", "revoked", "suspended"]

# The PassEvent detail this host records when a pushed lift clears its suspension; the refusal
# count that suspends a pass starts again from the newest one.
LIFTED = "unsuspended"


def pass_state(p: Pass, now: datetime | None = None) -> PassState:
    """The ingest projection's own reading: a pushed pass has no home collection here, so a null
    `collection_id` means nothing (home's `pass_state` would call every one of them revoked)."""
    if p.revoked_at is not None:
        return "revoked"
    if p.suspended_at is not None:
        return "suspended"
    if p.expires_at <= (now or datetime.now(UTC)):
        return "expired"
    return "active"


def device_active(device: PassDevice) -> bool:
    return device.revoked_at is None and device.token_hash is not None


# One lock for every cursor draw: holding it from the draw to the commit makes the order of the
# values the order of the commits, so `since=<n>` never skips a row that committed late.
_CHANGE_LOCK_KEY = 0x7472_6970  # "trip"


DISK_RESERVATION_LOCK = 0x6469736B  # 'disk'


async def next_change_seq(db: AsyncSession) -> int:
    """Draw a change cursor. Call it LAST in a transaction, right before its commit."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CHANGE_LOCK_KEY})
    return (await db.execute(select(func.nextval(INGEST_CHANGE_SEQ.name)))).scalar_one()


async def lock_disk_reservation(db: AsyncSession) -> None:
    """Serialize the volume-floor check and the session insert it admits, across all passes."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": DISK_RESERVATION_LOCK})


def free_bytes(root: Path) -> int:
    """Free bytes on the volume that holds `root` — the one probe of the disk floor."""
    root.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(root).free


async def lock_blob_hash(db: AsyncSession, sha256: str) -> None:
    """Serialize everything that writes or removes the physical copy of one hash."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:h, 0))"), {"h": f"blob:{sha256}"}
    )


# --- type allowlist and the fail-closed sniff --------------------------------------------------

# extension → (family, mime). The client's MIME is advisory only.
ALLOWED_EXTENSIONS: dict[str, tuple[str, str]] = {
    "jpg": ("jpeg", "image/jpeg"),
    "jpeg": ("jpeg", "image/jpeg"),
    "png": ("png", "image/png"),
    "heic": ("heif", "image/heic"),
    "heif": ("heif", "image/heic"),
    "dng": ("tiff", "image/x-adobe-dng"),
    "mov": ("isobmff", "video/quicktime"),
    "mp4": ("isobmff", "video/mp4"),
    "m4v": ("isobmff", "video/x-m4v"),
}

_QUICKTIME_ATOMS = frozenset({b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"})


def extension_of(filename: str) -> str | None:
    name = filename.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else None


def sniff_family(header: bytes) -> str | None:
    """What these bytes positively ARE, or None. Never falls back to anything the client said."""
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if detect_heif_mime(header) == "image/heic":
        return "heif"
    if len(header) >= 8 and header[4:8] == b"ftyp" and detect_heif_mime(header) is None:
        return "isobmff"
    if len(header) >= 8 and header[4:8] in _QUICKTIME_ATOMS:
        return "isobmff"
    return None


def sniff_matches(path: Path, filename: str) -> str | None:
    """The stored MIME when the bytes are the type the name claims, else None (→ 415)."""
    ext = extension_of(filename)
    if ext is None:
        return None
    family, mime = ALLOWED_EXTENSIONS[ext]
    try:
        with open(path, "rb") as f:
            header = f.read(64)
    except OSError:
        return None
    return mime if sniff_family(header) == family else None


# --- in-process limits -------------------------------------------------------------------------


class SlidingWindow:
    """At most `limit` events per `window_s` per key, in this process (one uvicorn worker)."""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self.events: dict[str, deque[float]] = {}

    def hit(self, key: str, limit: int, now: float | None = None) -> bool:
        """Record one event; False when the key is already at its limit (the event is refused)."""
        at = time.monotonic() if now is None else now
        q = self.events.setdefault(key, deque())
        while q and q[0] <= at - self.window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(at)
        return True

    def reset(self) -> None:
        self.events.clear()


redeem_window = SlidingWindow(60.0)


class BodyAdmission:
    """Guest upload bodies streaming right now, per device and in total (the member lane's
    admission, keyed on the device). No lock: nothing between check and increment awaits."""

    def __init__(self) -> None:
        self.per_device: dict[int, int] = {}
        self.total = 0

    def admit(self, device_id: int) -> bool:
        held = self.per_device.get(device_id, 0)
        if (
            held >= settings.GUEST_BODIES_PER_DEVICE
            or self.total >= settings.GUEST_BODIES_PER_PROCESS
        ):
            return False
        self.per_device[device_id] = held + 1
        self.total += 1
        return True

    def release(self, device_id: int) -> None:
        held = self.per_device[device_id] - 1
        if held:
            self.per_device[device_id] = held
        else:
            del self.per_device[device_id]
        self.total -= 1


body_admission = BodyAdmission()
