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
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from app.models import Attachment, AttachmentStatus, Memo, User, Visibility
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
                "SELECT attachment_id, kind, dropbox_path, shared_link, bytes, swept_at, "
                "uploaded_at FROM digest_hosted_media ORDER BY attachment_id"
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
    checks that something else is still making progress.
    """

    sent: list[SentMail] = field(default_factory=list)
    refusals_by_part: dict[int, dict[str, tuple[int, bytes]]] = field(default_factory=dict)
    raise_on_part: int | None = None
    delay: float = 0.0

    async def send(
        self, message: MIMEMultipart, sender: str, recipients: list[str]
    ) -> dict[str, tuple[int, bytes]]:
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
    """Dropbox, and nothing else. Records what a run asked it to upload and delete."""

    uploads: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    base_folder: str = "/memos-mail/run"
    closed: int = 0

    async def upload(self, local_path: Path, filename: str) -> tuple[str, str, int]:
        self.uploads.append((str(local_path), filename))
        dropbox_path = f"{self.base_folder}/{filename}"
        return dropbox_path, f"https://dropbox.example/{filename}?raw=1", 1234

    async def delete(self, dropbox_path: str) -> None:
        self.deletes.append(dropbox_path)

    async def aclose(self) -> None:
        # Counted, not ignored: "the client is released on every path" is an assertion, and a
        # no-op here would make it unfalsifiable.
        self.closed += 1


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
