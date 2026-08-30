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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from app.models import (
    Attachment,
    AttachmentStatus,
    DigestVisibilityScope,
    Memo,
    User,
    Visibility,
)
from app.services.digest.domain import DigestConfig
from app.services.digest.hosting import DropboxError
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


def add_recipients(*emails: str, enabled: bool = True) -> None:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        for email in emails:
            conn.execute(
                text(
                    "INSERT INTO digest_recipients (email, enabled) VALUES (:email, :enabled) "
                    "ON CONFLICT (email) DO UPDATE SET enabled = EXCLUDED.enabled"
                ),
                {"email": email, "enabled": enabled},
            )
    engine.dispose()


def run_rows() -> list[dict]:
    engine = create_engine(SYNC_URL)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, trigger, status, parts, parts_sent, parts_claimed, memos_sent, "
                "recipients, error, heartbeat_at, started_at, finished_at, local_date "
                "FROM digest_runs ORDER BY id"
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
                "SELECT memo_id, run_id, part_index, incomplete FROM digest_deliveries "
                "ORDER BY memo_id"
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
                "SELECT id, attachment_id, kind, dropbox_path, status, shared_link, bytes, "
                "created_at, uploaded_at, swept_at FROM digest_hosted_media ORDER BY id"
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
) -> Attachment:
    attachment = Attachment(
        uid=uid or secrets.token_urlsafe(8),
        owner_id=user.id,
        memo_id=memo.id,
        filename=filename,
        mime=mime,
        size=1,
        status=AttachmentStatus.ready,
    )
    db.add(attachment)
    await db.flush()
    return attachment


# --- derived-tier files -------------------------------------------------------------------------


def write_thumb(data_root: Path, uid: str, payload: bytes = b"thumb") -> Path:
    path = data_root / "assets" / "thumbs" / f"{uid}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_mobile(data_root: Path, uid: str, payload: bytes = b"mobile") -> Path:
    path = data_root / "assets" / "mobile" / f"{uid}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_web_mp4(data_root: Path, uid: str, payload: bytes = b"mp4") -> Path:
    path = data_root / "assets" / "web" / f"{uid}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_poster(data_root: Path, uid: str, payload: bytes = b"poster") -> Path:
    path = data_root / "assets" / "web" / f"{uid}.poster.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
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
        self.sent.append(
            SentMail(message=message, sender=sender, recipients=list(recipients))
        )
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
