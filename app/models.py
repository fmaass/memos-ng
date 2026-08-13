import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Visibility(enum.StrEnum):
    PRIVATE = "PRIVATE"
    PROTECTED = "PROTECTED"
    PUBLIC = "PUBLIC"


class AttachmentStatus(enum.StrEnum):
    uploading = "uploading"
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class JobLane(enum.StrEnum):
    fast = "fast"
    slow = "slow"


class JobState(enum.StrEnum):
    queued = "queued"
    claimed = "claimed"
    processing = "processing"
    done = "done"
    failed = "failed"


memo_tags = Table(
    "memo_tags",
    Base.metadata,
    Column("memo_id", ForeignKey("memos.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Authelia preferred_username — stable identity across logins.
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(32), default="user")
    avatar: Mapped[str | None] = mapped_column(Text)  # data: URL or path (migrated from prod)
    # Per-user settings (prod parity). default_visibility seeds new memos; theme is
    # server-persisted so it follows the account across devices.
    default_visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, name="visibility"), default=Visibility.PUBLIC
    )
    theme: Mapped[str] = mapped_column(String(32), default="dark")
    # Retained (nullable, now UNUSED) after the SMTP frame-send path and per-user frame settings
    # were removed — #curated delivery is now a system-level album HTTP upload. Kept for one
    # rollback release; nothing reads or writes these columns.
    frame_send_to: Mapped[str | None] = mapped_column(String(320))
    frame_send_from: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Memo(Base):
    __tablename__ = "memos"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable public id preserved from old memos (base62) — unique-indexed.
    uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, name="visibility"), default=Visibility.PRIVATE, index=True
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # selectin so async serialization can read tags/owner without a lazy-load error
    tags: Mapped[list["Tag"]] = relationship(
        secondary=memo_tags, back_populates="memos", lazy="selectin"
    )
    owner: Mapped["User"] = relationship(lazy="selectin")
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", lazy="selectin", order_by="Attachment.id"
    )
    reactions: Mapped[list["Reaction"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan"
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    memos: Mapped[list["Memo"]] = relationship(secondary=memo_tags, back_populates="tags")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    memo_id: Mapped[int | None] = mapped_column(
        ForeignKey("memos.id", ondelete="SET NULL"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None]
    blurhash: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)  # content dedup
    # Original capture time (EXIF/QuickTime → File.lastModified → upload time).
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[AttachmentStatus] = mapped_column(
        Enum(AttachmentStatus, name="attachment_status"),
        default=AttachmentStatus.uploading,
        index=True,
    )
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    # Optional print-quality rendition (a separately-uploaded Lightroom print export): same
    # logical photo, hidden from display, download-only. NOT derivable from the web master —
    # output sharpening + color profile differ per medium (ADR: web/print renditions).
    print_path: Mapped[str | None] = mapped_column(String(1024))
    print_mime: Mapped[str | None] = mapped_column(String(255))
    print_size: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Hidden, download-only extra files for this logical photo (e.g. print masters). NOT Attachment
    # rows, so they never surface in any feed/library/compat query — see AttachmentRendition.
    renditions: Mapped[list["AttachmentRendition"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan"
    )


class AttachmentRendition(Base):
    """A hidden, download-only physical file for a logical photo — generalizes the single legacy
    print_path into many named variants (e.g. print:glossy, print:matte). The SCREEN file stays the
    Attachment (it owns its renditions); a rendition is deliberately NOT an Attachment row, so the
    web feed/library/compat/SSE/export — which only ever traverse Attachment rows — cannot surface
    it. Renditions carry no derived tiers and get no MediaJob (download-only archive masters)."""

    __tablename__ = "attachment_renditions"
    __table_args__ = (
        UniqueConstraint("attachment_id", "variant_key", name="uq_attachment_rendition"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), index=True
    )
    variant_key: Mapped[str] = mapped_column(String(96))  # "print" or "print:<slug>"
    label: Mapped[str | None] = mapped_column(String(48))  # slugified sub-name (glossy/matte)
    filename: Mapped[str] = mapped_column(String(512))  # original dumped name (round-trip fidelity)
    mime: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))  # informational only (never file-shared)
    storage_path: Mapped[str] = mapped_column(String(1024))  # a unique uuid-named file under print/
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Upload(Base):
    __tablename__ = "uploads"

    upload_uid: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The session's creating user. Every verb (HEAD/append/finalize) is scoped to it, so a uid alone
    # is not an authorization — knowing someone else's uid buys nothing. Nullable ONLY for rows
    # predating the owner column: those have no verifiable creator, match no owner, and so fail
    # closed on every verb until the expiry reaper removes them.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(255))
    declared_size: Mapped[int] = mapped_column(BigInteger)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    client_mtime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # File.lastModified
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaJob(Base):
    __tablename__ = "media_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), index=True
    )
    lane: Mapped[JobLane] = mapped_column(Enum(JobLane, name="job_lane"))
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state"), default=JobState.queued, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FrameShareState(enum.StrEnum):
    queued = "queued"
    claimed = "claimed"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class FrameShareJob(Base):
    """One durable job per image attachment to email to the user's Aura frame. UNIQUE on
    attachment_id is the at-most-once idempotency guard: re-editing a #curated memo cannot resend.
    FKs CASCADE so a deleted attachment/memo/user removes the job — but attachments.memo_id is
    ON DELETE SET NULL, so a job can outlive its memo link; the worker revalidates at send time."""

    __tablename__ = "frame_share_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), unique=True, index=True
    )
    memo_id: Mapped[int] = mapped_column(ForeignKey("memos.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    state: Mapped[FrameShareState] = mapped_column(
        Enum(FrameShareState, name="frame_share_state"),
        default=FrameShareState.queued,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebSession(Base):
    """BFF server-side session: tokens stay here, the browser holds only the sid cookie."""

    __tablename__ = "sessions"

    sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    encrypted_tokens: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    absolute_expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Pat(Base):
    __tablename__ = "pats"

    id: Mapped[int] = mapped_column(primary_key=True)
    hashed_token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reaction(Base):
    __tablename__ = "reactions"
    __table_args__ = (UniqueConstraint("memo_id", "user_id", "emoji", name="uq_reaction"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    memo_id: Mapped[int] = mapped_column(ForeignKey("memos.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    emoji: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # A push endpoint is one device's mailbox — globally unique, so re-subscribe upserts.
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
