"""Seeding, fakes and file helpers shared by the digest tests.

A module rather than conftest fixtures on purpose: only the digest tests need any of it, and a
global fixture would pull `digest_*` reset behaviour into every other suite. Nothing here is a
fixture, so each test decides what it wants.

**Every fake here stands in for a DEPENDENCY, never for the unit under test.** `FakeTransport` is
the mail server; `FakeHost` is Dropbox. The store, the runner, the splitter and the renderer are
always the real ones — a fake in their place would prove nothing about the commit boundary, the
claim CAS or the CID accounting, which is exactly what these tests exist to prove.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from io import BytesIO
from pathlib import Path

from app.models import (
    Attachment,
    AttachmentStatus,
    DigestVisibilityScope,
    MailSubsampling,
    Memo,
    User,
    Visibility,
)
from app.services.digest.domain import DigestConfig
from app.services.digest.hosting import DropboxError
from app.services.export_settings import MailProfile
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import SYNC_URL

# --- database reset -----------------------------------------------------------------------------

DIGEST_TABLES = (
    "digest_deliveries",
    "digest_hosted_media",
    "digest_runs",
    "digest_recipients",
)


def digest_config(**overrides) -> DigestConfig:
    """A settings snapshot carrying the migration's own defaults, with named overrides.

    The pipeline reads `DigestConfig`, never a mapped row, so this is the honest input for every
    test that does not need the database to have an opinion.
    """
    values = {
        "enabled": True,
        "send_time": time(7, 0),
        "timezone": "Europe/Zurich",
        "visibility_scope": DigestVisibilityScope.PUBLIC_PROTECTED,
        "start_date": date(2026, 6, 1),
        "grace_minutes": 30,
        "max_email_bytes": 18874368,
        "max_videos_per_memo": 2,
        "max_memos_per_run": 40,
        "mail_title": "The Daily Dami Memos Mail",
        "from_name": "The Daily Dami Memos Mail",
        "from_email": "",
        "hosting_enabled": False,
        "dropbox_folder": "/memos-mail",
        "retention_days": 30,
        "link_images_full": False,
    }
    values.update(overrides)
    return DigestConfig(**values)  # pyright: ignore[reportArgumentType]


def mail_profile(**overrides) -> MailProfile:
    """The mail profile carrying the migration's own defaults, with named overrides.

    The renderer is handed a profile as a VALUE (the run reads settings once), so this is the
    honest input for every test that does not need the database to have an opinion about it.
    """
    values = {
        "long_edge": 1600,
        "jpeg_quality": 80,
        "subsampling": MailSubsampling.C420,
        "progressive": False,
    }
    values.update(overrides)
    return MailProfile(**values)  # pyright: ignore[reportArgumentType]


class BorrowedSession:
    """Hands an EXISTING session to code that opens its own, without closing it.

    The `adb` fixture binds its session to a transaction the test rolls back, so a second real
    session would not see anything the test seeded. This lets such a test drive a function that
    takes a session factory while keeping the fixture's isolation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def borrowed_session_factory(session: AsyncSession):
    return lambda: BorrowedSession(session)


class TrackingSessionFactory:
    """`async_session`, plus a count of how many of ITS sessions currently hold a transaction.

    This is the mechanism behind "no transaction is open across an external boundary": the fakes
    that stand in for Dropbox and SMTP ask it, at the moment they are called, how many sessions the
    code under test is holding a transaction in. A SQLAlchemy `SELECT` autobegins one, so a session
    merely left open after a read still answers True — which is exactly the state being ruled out.
    """

    def __init__(self, factory=None) -> None:
        from app.database import async_session

        self._factory = factory or async_session
        self._sessions: list[AsyncSession] = []

    def __call__(self) -> AsyncSession:
        session = self._factory()
        self._sessions.append(session)
        return session

    def live_transactions(self) -> int:
        return sum(1 for session in self._sessions if session.in_transaction())

    def assert_no_transaction(self, where: str) -> None:
        live = self.live_transactions()
        assert live == 0, f"{live} database transaction(s) open across {where}"


def reset_digest_state(**settings_columns: object) -> None:
    """Empty every digest table and re-seed the settings singleton the way the migration does.

    These tests drive code that COMMITS, so state left behind would decide the next test's
    single-flight arithmetic. Named columns are applied on top of the migration's own defaults, so
    a test states only what it changes.
    """
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        for table in DIGEST_TABLES:
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM digest_settings"))
        conn.execute(text("INSERT INTO digest_settings (id) VALUES (1)"))
        for column, value in settings_columns.items():
            conn.execute(
                text(f"UPDATE digest_settings SET {column} = :value WHERE id = 1"),
                {"value": value},
            )
    engine.dispose()


def set_digest_settings(**columns: object) -> None:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        for column, value in columns.items():
            conn.execute(
                text(f"UPDATE digest_settings SET {column} = :value WHERE id = 1"),
                {"value": value},
            )
    engine.dispose()


def set_export_settings(**columns: object) -> None:
    """Write columns of the `export_settings` singleton, COMMITTED.

    Separate from `set_digest_settings` because the two rows answer different questions and the
    digest reads them from different modules — and because a test that changes the mail profile
    MID-RUN needs a write that a running pipeline can actually see.
    """
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        for column, value in columns.items():
            conn.execute(
                text(f"UPDATE export_settings SET {column} = :value WHERE id = 1"),
                {"value": value},
            )
    engine.dispose()


def reset_export_settings() -> None:
    """The export singleton back to the migration's defaults, the way the migration seeds it."""
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM export_settings"))
        conn.execute(text("INSERT INTO export_settings (id) VALUES (1)"))
    engine.dispose()


def add_recipients(*emails: str, enabled: bool = True, language: str = "en") -> None:
    """`language` defaults to English, the language every pre-MEMNG-1245 test asserts its bytes
    in; the column's own default (de) is asserted by `test_digest_language.py`."""
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        for email in emails:
            conn.execute(
                text(
                    "INSERT INTO digest_recipients (email, enabled, language) "
                    "VALUES (:email, :enabled, :language) "
                    "ON CONFLICT (email) DO UPDATE SET enabled = EXCLUDED.enabled, "
                    "language = EXCLUDED.language"
                ),
                {"email": email, "enabled": enabled, "language": language},
            )
    engine.dispose()


def run_rows() -> list[dict]:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, trigger, status, parts, parts_sent, parts_claimed, memos_sent, "
                "recipients, error, heartbeat_at, started_at, finished_at, local_date, "
                "groups_attempted, groups_accepted FROM digest_runs ORDER BY id"
            )
        ).mappings()
        result = [dict(row) for row in rows]
    engine.dispose()
    return result


def delivery_rows() -> list[dict]:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT memo_id, language, run_id, part_index, incomplete "
                "FROM digest_deliveries ORDER BY memo_id, language"
            )
        ).mappings()
        result = [dict(row) for row in rows]
    engine.dispose()
    return result


def hosted_rows() -> list[dict]:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, attachment_id, run_id, kind, dropbox_path, status, shared_link, "
                "bytes, created_at, uploaded_at, swept_at FROM digest_hosted_media ORDER BY id"
            )
        ).mappings()
        result = [dict(row) for row in rows]
    engine.dispose()
    return result


def age_run(run_id: int, *, heartbeat_minutes: int | None, started_minutes: int) -> None:
    """Push a run's stamps into the past. The sweep's bar is DB-clock relative, so this is how a
    test says "this run last beat 20 minutes ago" without touching a clock."""
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE digest_runs SET started_at = now() - make_interval(mins => :started), "
                "heartbeat_at = CASE WHEN :beat IS NULL THEN NULL "
                "ELSE now() - make_interval(mins => :beat) END WHERE id = :id"
            ),
            {"id": run_id, "started": started_minutes, "beat": heartbeat_minutes},
        )
    engine.dispose()


def age_hosted_media(attachment_id: int, *, days: int) -> None:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE digest_hosted_media SET uploaded_at = now() - make_interval(days => :d) "
                "WHERE attachment_id = :id"
            ),
            {"id": attachment_id, "d": days},
        )
    engine.dispose()


def insert_run(*, trigger: str = "manual", status: str | None = None, local_date=None) -> int:
    """One `digest_runs` row written directly, for the owner states a ledger test needs."""
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        run_id = conn.execute(
            text(
                "INSERT INTO digest_runs (trigger, status, local_date, heartbeat_at) "
                "VALUES (:trigger, :status, :day, now()) RETURNING id"
            ),
            {"trigger": trigger, "status": status, "day": local_date or date(2026, 7, 1)},
        ).scalar_one()
    engine.dispose()
    return int(run_id)


def age_hosted_claim(ledger_id: int, *, minutes: int) -> None:
    """Push a PENDING row's `created_at` into the past — how a test says "this intention has been
    sitting unresolved long enough for the sweep to go and ask Dropbox about it"."""
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE digest_hosted_media SET created_at = now() - make_interval(mins => :m) "
                "WHERE id = :id"
            ),
            {"id": ledger_id, "m": minutes},
        )
    engine.dispose()


def insert_hosted_row(**columns: object) -> int:
    """One ledger row written directly, for the states a test needs to START in."""
    engine = create_engine(SYNC_URL)
    values = {
        "attachment_id": None,
        "run_id": None,
        "kind": "video",
        "status": "uploaded",
        "shared_link": None,
        "bytes": 0,
        **columns,
    }
    names = ", ".join(values)
    binds = ", ".join(f":{name}" for name in values)
    with engine.begin() as conn:
        row_id = conn.execute(
            text(f"INSERT INTO digest_hosted_media ({names}) VALUES ({binds}) RETURNING id"),
            values,
        ).scalar_one()
    engine.dispose()
    return int(row_id)


# --- seeding ------------------------------------------------------------------------------------


async def seed_user(db: AsyncSession, *, subject: str | None = None) -> User:
    user = User(subject=subject or f"digest-{secrets.token_hex(4)}", role="user")
    db.add(user)
    await db.flush()
    return user


async def seed_memo(
    db: AsyncSession,
    user: User,
    *,
    content: str = "",
    visibility: Visibility = Visibility.PUBLIC,
    created_at: datetime | None = None,
    uid: str | None = None,
    archived: bool = False,
) -> Memo:
    memo = Memo(
        uid=uid or secrets.token_urlsafe(8),
        owner_id=user.id,
        content=content,
        visibility=visibility,
        created_at=created_at or datetime.now(UTC),
        archived_at=datetime.now(UTC) if archived else None,
    )
    db.add(memo)
    await db.flush()
    return memo


async def seed_attachment(
    db: AsyncSession,
    user: User,
    memo: Memo,
    *,
    mime: str = "image/jpeg",
    uid: str | None = None,
    filename: str = "photo.jpg",
    storage_path: str | None = None,
) -> Attachment:
    attachment = Attachment(
        uid=uid or secrets.token_urlsafe(8),
        owner_id=user.id,
        memo_id=memo.id,
        filename=filename,
        mime=mime,
        size=1,
        status=AttachmentStatus.ready,
        storage_path=storage_path,
    )
    db.add(attachment)
    await db.flush()
    return attachment


# --- derived-tier files -------------------------------------------------------------------------


def jpeg_bytes(size: tuple[int, int] = (240, 180), *, noise: bool = False) -> bytes:
    """A REAL JPEG of `size`, because the digest now DECODES what it finds on disk.

    `b"thumb"` was an honest fixture while the pipeline copied a tier's bytes into the message
    untouched; it stopped being one the moment the renderer re-encoded them
    (`media.encode_for_mail`), because a placeholder that will not decode exercises only the
    failure branch. Every tier helper below therefore writes a decodable image.

    `noise=True` fills it with incompressible random pixels — the honest shape for anything that
    measures BYTES, since a flat colour compresses to almost nothing at any quality and would make
    a size comparison meaningless.
    """
    width, height = size
    if noise:
        image = Image.frombytes("RGB", size, secrets.token_bytes(width * height * 3))
    else:
        # A gradient rather than a flat fill: it survives chroma subsampling with something left
        # to measure, and it is not degenerate for the encoder.
        image = Image.new("RGB", size)
        image.putdata(
            [
                ((x * 255) // max(1, width - 1), (y * 255) // max(1, height - 1), 128)
                for y in range(height)
                for x in range(width)
            ]
        )
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def write_mobile(
    data_root: Path,
    uid: str,
    payload: bytes | None = None,
    *,
    size: tuple[int, int] = (2048, 1536),
    noise: bool = False,
) -> Path:
    """The `mobile` tier — the file the digest inlines FROM (`select.MAIL_IMAGE_TIER`)."""
    path = data_root / "assets" / "mobile" / f"{uid}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes(size, noise=noise) if payload is None else payload)
    return path


def write_original(
    data_root: Path,
    uid: str,
    *,
    size: tuple[int, int] = (5000, 3000),
    noise: bool = False,
) -> Path:
    """The archive master a print rendition is DERIVED from (`printready.source_path`).

    Not a derived tier: the print tier is never made from a screen tier, so a test about the
    hosted full-size copy has to put a real original on disk and name it on the attachment row.
    """
    path = data_root / "originals" / f"{uid}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes(size, noise=noise))
    return path


def write_web_mp4(data_root: Path, uid: str, payload: bytes = b"mp4") -> Path:
    path = data_root / "assets" / "web" / f"{uid}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_poster(
    data_root: Path,
    uid: str,
    payload: bytes | None = None,
    *,
    size: tuple[int, int] = (1280, 720),
    noise: bool = False,
) -> Path:
    path = data_root / "assets" / "web" / f"{uid}.poster.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes(size, noise=noise) if payload is None else payload)
    return path


# --- fakes --------------------------------------------------------------------------------------


@dataclass
class SentMail:
    message: MIMEMultipart
    sender: str
    recipients: list[str]


@dataclass
class FakeTransport:
    """The mail server, and nothing else.

    `refusals_by_part` maps a 1-based part number to the refusal dict smtplib would return;
    `raise_on_part` makes that part's send fail outright (a connection error), which is a
    different case and has a different outcome. `delay` lets a test hold a send open while it
    checks that something else is still making progress. `sessions`, when set, is asked at the
    moment of the send whether the caller is holding a database transaction open across it.
    """

    sent: list[SentMail] = field(default_factory=list)
    refusals_by_part: dict[int, dict[str, tuple[int, bytes]]] = field(default_factory=dict)
    raise_on_part: int | None = None
    delay: float = 0.0
    sessions: TrackingSessionFactory | None = None
    # Called with the 1-based part number AFTER that part is accepted. A seam for the one thing
    # that cannot be staged any other way: something changing in the world part-way through a run.
    on_sent: Callable[[int], None] | None = None

    async def send(
        self, message: MIMEMultipart, sender: str, recipients: list[str]
    ) -> dict[str, tuple[int, bytes]]:
        if self.sessions is not None:
            self.sessions.assert_no_transaction("an SMTP send")
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        part = len(self.sent) + 1
        if self.raise_on_part == part:
            raise OSError("simulated SMTP connection failure")
        self.sent.append(SentMail(message=message, sender=sender, recipients=list(recipients)))
        if self.on_sent is not None:
            self.on_sent(part)
        return self.refusals_by_part.get(part, {})


@dataclass
class FakeHost:
    """Dropbox, and nothing else. Records what a run asked it to upload, probe and delete.

    `sessions`, when set, is asked at the moment of every remote call whether the caller is
    holding a database transaction open across it — which is how "no transaction crosses an
    external boundary" is asserted rather than asserted-about.

    `raise_on_upload` fails the upload of a named file the way Dropbox would, which is what leaves
    a ledger row `pending`; `absent` names paths the reconciliation probe should not find.
    """

    uploads: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    base_folder: str = "/memos-mail/run"
    closed: int = 0
    sessions: TrackingSessionFactory | None = None
    raise_on_upload: set[str] = field(default_factory=set)
    absent: set[str] = field(default_factory=set)
    # Shared with a ledger fake when a test cares about the ORDER of the two.
    events: list[str] = field(default_factory=list)

    def dropbox_path_for(self, filename: str) -> str:
        return f"{self.base_folder}/{filename}"

    async def upload(self, local_path: Path, dropbox_path: str) -> tuple[str, int]:
        if self.sessions is not None:
            self.sessions.assert_no_transaction("a Dropbox upload")
        filename = dropbox_path.rsplit("/", 1)[-1]
        self.events.append(f"upload:{dropbox_path}")
        if filename in self.raise_on_upload:
            raise DropboxError(f"simulated upload failure for {filename}")
        self.uploads.append((str(local_path), filename))
        return f"https://dropbox.example/{filename}?raw=1", 1234

    async def delete(self, dropbox_path: str) -> None:
        if self.sessions is not None:
            self.sessions.assert_no_transaction("a Dropbox delete")
        self.deletes.append(dropbox_path)

    async def remote_exists(self, dropbox_path: str) -> bool:
        if self.sessions is not None:
            self.sessions.assert_no_transaction("a Dropbox metadata probe")
        self.probes.append(dropbox_path)
        return dropbox_path not in self.absent

    async def shared_link(self, dropbox_path: str) -> str:
        filename = dropbox_path.rsplit("/", 1)[-1]
        return f"https://dropbox.example/{filename}?raw=1"

    async def aclose(self) -> None:
        # Counted, not ignored: "the client is released on every path" is an assertion, and a
        # no-op here would make it unfalsifiable.
        self.closed += 1


@dataclass
class RecordingLedger:
    """The hosted-media ledger as the hosting layer sees it — in memory.

    These tests are about the ORDER of the three steps (claim, upload, mark) and about which files
    each one names; the SQL behind the real ledger is exercised against a real database elsewhere.
    Sharing `events` with a `FakeHost` is what makes the order assertable rather than assumed.
    """

    claims: list[tuple[int, str, str]] = field(default_factory=list)
    marked: list[tuple[int, str, int]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    async def claim(self, *, attachment_id: int, kind: str, dropbox_path: str) -> int:
        self.claims.append((attachment_id, kind, dropbox_path))
        self.events.append(f"claim:{dropbox_path}")
        return len(self.claims)

    async def mark_uploaded(self, ledger_id: int, *, shared_link: str, size: int) -> None:
        self.marked.append((ledger_id, shared_link, size))
        self.events.append(f"mark:{ledger_id}")


def forbid_real_smtp(monkeypatch) -> None:
    """Make a real SMTP connection impossible for the rest of the test.

    A missed transport seam would otherwise open a socket to `smtp.mail.me.com` from the test
    suite — a real network call in a test is a defect, and one that fails as a 500 rather than as
    a named error is a defect that hides. This turns it into an obvious one.
    """
    import smtplib

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "a test reached the REAL smtplib transport — substitute it with FakeTransport"
        )

    monkeypatch.setattr(smtplib, "SMTP", _refuse)


def host_factory_for(host: object | None):
    """A `HostFactory` that hands back `host`, ignoring the heartbeat callback it is given."""

    async def factory(_on_operation):
        return host

    return factory


# --- an independent JPEG oracle -------------------------------------------------------------------
#
# Pillow encodes every image this codebase produces, so asking Pillow what it encoded is a
# conversation the encoder has with itself: it would stay green with `subsampling=` and
# `progressive=` deleted from the call and the library's own defaults answering instead (measured
# — the mutation was run). This reads the bytes a recipient would receive, segment by segment, and
# knows nothing about how they were made.
#
# The JPEG container is a chain of `FF <marker> <2-byte length> <payload>` after the `FFD8` start
# of image. Three segments carry everything asserted anywhere:
#
#   SOF0 (C0) / SOF1 (C1)  a BASELINE frame; SOF2 (C2) a progressive one — the distinction legacy
#                          Outlook cannot decode past;
#   the SOF payload        precision, height, width, component count, then per component
#                          `id, (h << 4) | v, quantisation table` — the sampling factors ARE the
#                          chroma subsampling;
#   APP2 (E2)              the ICC profile, whose payload starts `ICC_PROFILE\0`.
#
# Parsing stops at SOS (DA): everything after it is entropy-coded scan data with no segment
# structure, and nothing needs it.

_STANDALONE = {0xD8, 0x01, *range(0xD0, 0xD8)}  # markers that carry no length field
_SOF_BASELINE = {0xC0, 0xC1}
_SOF_PROGRESSIVE = {0xC2}


@dataclass(frozen=True)
class JpegFacts:
    """What the bytes on the wire say about themselves."""

    width: int
    height: int
    progressive: bool
    sampling: tuple[tuple[int, int], ...]
    has_icc: bool

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)

    @property
    def subsampling(self) -> str:
        """`4:2:0` or `4:4:4` in the notation the settings row uses, or a description of neither.

        Read off the luma component's sampling factors against the chroma planes', which is what
        the notation means: 2x2 luma samples per chroma sample is 4:2:0, 1x1 is 4:4:4.
        """
        if len(self.sampling) != 3:
            return f"{len(self.sampling)}-component"
        (luma_h, luma_v), *chroma = self.sampling
        if any(pair != (1, 1) for pair in chroma):
            return "irregular"
        if (luma_h, luma_v) == (2, 2):
            return MailSubsampling.C420
        if (luma_h, luma_v) == (1, 1):
            return MailSubsampling.C444
        return f"{luma_h}x{luma_v}"


def jpeg_facts(data: bytes) -> JpegFacts:
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    index = 2
    has_icc = False
    while index < len(data):
        assert data[index] == 0xFF, f"lost segment alignment at byte {index}"
        marker = data[index + 1]
        index += 2
        if marker in _STANDALONE:
            continue
        length = int.from_bytes(data[index : index + 2], "big")
        payload = data[index + 2 : index + length]
        if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
            has_icc = True
        elif marker in _SOF_BASELINE or marker in _SOF_PROGRESSIVE:
            components = payload[5]
            return JpegFacts(
                width=int.from_bytes(payload[3:5], "big"),
                height=int.from_bytes(payload[1:3], "big"),
                progressive=marker in _SOF_PROGRESSIVE,
                sampling=tuple(
                    (payload[7 + i * 3] >> 4, payload[7 + i * 3] & 0x0F) for i in range(components)
                ),
                has_icc=has_icc,
            )
        elif marker == 0xDA:  # start of scan: no frame header was found before the pixels
            break
        index += length
    raise AssertionError("the JPEG carries no frame header")


def inline_jpegs(message: MIMEMultipart) -> list[JpegFacts]:
    """Every `image/jpeg` part of a SERIALISED message, decoded from its transfer encoding.

    Serialised and re-parsed rather than read off the objects a run built: base64 is where a
    message stops being a Python structure, and what arrives is the thing under test.
    """
    parsed = message_from_bytes(message.as_bytes())
    facts = []
    for part in parsed.walk():
        if part.get_content_type() == "image/jpeg":
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            facts.append(jpeg_facts(payload))
    return facts


def message_content_types(message: MIMEMultipart) -> list[str]:
    return [part.get_content_type() for part in message.walk()]


def html_part(message: MIMEMultipart) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            return payload.decode("utf-8")
    raise AssertionError("the message carries no text/html part")


def plain_part(message: MIMEMultipart) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            return payload.decode("utf-8")
    raise AssertionError("the message carries no text/plain part")


def content_ids(message: MIMEMultipart) -> list[str]:
    return [
        part["Content-ID"].strip("<>")
        for part in message.walk()
        if part.get("Content-ID") is not None
    ]
