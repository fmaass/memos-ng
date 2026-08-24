import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
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


class JobKind(enum.StrEnum):
    """WHAT a media job derives, orthogonal to the lane that decides WHERE it runs.

    Until MEMNG-49 every job was the upload pipeline and the work was inferred from lane+mime, so
    a second kind of image work on the slow lane had no way to announce itself. `derive` is that
    original pipeline (tiers/poster/dims/blurhash, and the transcode it chains); `printready` is
    the on-demand 4096 px print rendition, which is never produced by an upload — only by an
    explicit enqueue (app/services/printready.py).

    Deliberately a free string column rather than a Postgres enum: a new kind then costs no
    ALTER TYPE (which cannot run in a transaction alongside its own use) and no migration at all.
    """

    derive = "derive"
    printready = "printready"


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

    __table_args__ = (
        # PARTIAL unique index (MEMNG-110): a key is unique per owner, and the WHERE clause keeps
        # every key-less memo out of it — every memo predating this column, and every one posted by
        # a client that sends no key, would otherwise collide on a single NULL-bearing entry in
        # some engines and cost an index entry each here. Uniqueness is also the concurrency
        # settlement: two racing creates with one key cannot both insert, so the loser reads the
        # winner back instead of a second memo existing.
        Index(
            "uq_memos_owner_idempotency_key",
            "owner_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

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
    # Client-supplied retry token (MEMNG-110). NULL for every memo posted without one — the column
    # records what the client called this create, and nothing else reads it.
    idempotency_key: Mapped[str | None] = mapped_column(String(64))

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


# Named, and named ONCE, because the router has to recognise this constraint by name at runtime:
# only a violation of THIS constraint means "a same-key edit got there first" and may be answered
# with a replay. Any other integrity error belongs to the caller's own write and must surface.
EDIT_KEY_CONSTRAINT = "uq_memo_edit_keys_owner_key"


class MemoEditKey(Base):
    """One client-supplied `edit_key` that has already been applied to a memo (MEMNG-124).

    The create side records its retry token ON the memo (`Memo.idempotency_key`) because a create
    produces exactly one memo. An edit cannot: a memo is edited many times, so the token belongs in
    its own row, and this table is the ledger of "this owner's edit K already landed on that memo".

    A separate table rather than a column also keeps the semantics honest: the row records THAT the
    edit landed, not what the memo looked like afterwards. A replay therefore answers with the
    memo's CURRENT state — see `services.memos.get_memo_by_edit_key`.

    `memo_id` cascades, so deleting a memo takes its recorded edits with it: rows pointing at a memo
    nobody has are useless, and keeping them would hold a key hostage against a later legitimate
    edit. Uniqueness is per OWNER, not global — the key is the client's name for its own edit, so
    two phones minting the same token must not collide — and it is also the concurrency settlement:
    two racing patches carrying one key cannot both insert, so the loser reads the winner back
    instead of the edit being applied twice."""

    __tablename__ = "memo_edit_keys"

    __table_args__ = (UniqueConstraint("owner_id", "edit_key", name=EDIT_KEY_CONSTRAINT),)

    id: Mapped[int] = mapped_column(primary_key=True)
    memo_id: Mapped[int] = mapped_column(ForeignKey("memos.id", ondelete="CASCADE"), index=True)
    # No index=True on owner_id: uq_memo_edit_keys_owner_key is a btree LEADING with owner_id, so it
    # already serves both the replay lookup and the cascade when a user is deleted. A second index
    # over the same column would be paid for on every write and read by nothing.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    edit_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # order_by id: the API serializes this list as-is and the UI names the FIRST print variant "the
    # print master", which must be the same row `printready.print_rendition` derives from (lowest
    # id = earliest attached). Without an explicit order that agreement rests on Postgres's physical
    # row order, which nothing guarantees after an UPDATE moves a row.
    renditions: Mapped[list["AttachmentRendition"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="AttachmentRendition.id"
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
    # is not an authorization — knowing someone else's uid buys nothing. Nullable for rows predating
    # the owner column: those have no verifiable creator, match no owner, and so fail closed on
    # every verb until the expiry reaper removes them.
    # SET NULL, not CASCADE: reap_expired unlinks a .part file only by walking its uploads row (the
    # orphan sweep matches *.spool only), so cascading the row away on user deletion would strand
    # the bytes on disk forever. Nulling the owner lands the row in that same fail-closed state and
    # lets the reaper collect row AND part normally.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
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
    """One unit of derivation work for an attachment (a queue row, not a data row).

    `attachment_id` carries NO foreign key on purpose (MEMNG-41). A worker claims a job with
    SELECT … FOR UPDATE and holds that row lock until the job commits — for the slow lane that is
    the whole transcode, up to SLOW_TRANSCODE_TIMEOUT_S. An FK's ON DELETE CASCADE would make
    `DELETE FROM attachments` lock the same child row, so deleting a photo whose video was being
    transcoded blocked a request-serving connection for up to an hour (with DB_POOL_SIZE 5 on a
    shared Postgres, a few of those starve the app). The cascade was also redundant: the delete
    endpoint already removes the job rows explicitly. It now removes every job row nobody is
    running (SKIP LOCKED) and leaves the in-flight one to its worker, which discards it on
    discovering the attachment is gone (media.AttachmentGone → worker.run_once).
    """

    __tablename__ = "media_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(Integer, index=True)
    lane: Mapped[JobLane] = mapped_column(Enum(JobLane, name="job_lane"))
    # See JobKind: a plain string, so the value set can grow without a migration. The server
    # default is what makes the column additive — every job row written before MEMNG-49 (and any
    # INSERT that predates this column in a rolling deploy) reads back as the upload pipeline.
    # Not indexed: every kind lookup is already scoped by attachment_id (indexed) or by the
    # state+lane claim index, and media_jobs is a write-heavy queue where an index nothing selects
    # on is pure insert cost.
    kind: Mapped[str] = mapped_column(
        String(32), default=JobKind.derive, server_default=JobKind.derive
    )
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


class MediaJobProgress(Base):
    """The latest progress sample of a RUNNING media job — a side channel, deliberately not a
    column on `media_jobs` (MEMNG-36).

    A worker claims its job with SELECT … FOR UPDATE and holds that row lock for the WHOLE job:
    claim and outcome share one transaction on purpose, so a crash rolls the claim back to
    `queued` (MEMNG-39). Two consequences make the job row unusable as a progress channel — the
    percentages the worker writes into it are invisible to every other session until that final
    commit, and any other session UPDATEing the claimed row would wait on the row lock for the
    length of a transcode (the same wait MEMNG-41 removed from the delete path). This row is
    written and COMMITTED by a short session of its own instead, so `/api/events` can read a
    transcode's real progress while it runs.

    `job_id` carries no FK — same reason media_jobs dropped its attachment FK: a cascade would
    make a delete wait on precisely the locks this table exists to sidestep. The row is transient:
    the transcode clears it when it stops running, and the worker sweeps any that a crash left
    behind (worker.sweep_job_progress_safe), so nothing accumulates.
    """

    __tablename__ = "media_job_progress"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    progress_pct: Mapped[int] = mapped_column(Integer)
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
    # Lease stamp written when the worker commits the `claimed` tombstone (MEMNG-35). The recovery
    # sweep re-queues a job still `claimed` past the stale horizon — without it a crash between the
    # tombstone commit and the upload result stranded the delivery in `claimed` forever. NULL on a
    # row claimed before this column existed, which the sweep reads as "long stranded".
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # NULL = never expires, which is what every token minted before this column existed is.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NativeAuthCode(Base):
    """A one-shot browser→app handoff code from a native device login.

    Nothing here is a credential on its own: the row is useless without the PKCE verifier only the
    initiating app holds, and only the sha256 of the code is stored — so a database read cannot be
    replayed into a token. Rows are short-lived (`expires_at`), single-use (`consumed_at`) and
    swept an hour past expiry by the next mint.
    """

    __tablename__ = "native_auth_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The APP's PKCE challenge, not the BFF's — it binds this code to the app instance that
    # started the login, so a code intercepted in flight cannot be exchanged by anyone else.
    code_challenge: Mapped[str] = mapped_column(String(128))
    device_name: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
