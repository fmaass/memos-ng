import enum
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

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


# Named, and named ONCE, for the same reason as the edit key below: the router has to recognise
# this constraint by name at runtime. When a keyed create runs out of retries, only a violation of
# THIS constraint means "a same-key create kept getting there first" and may be answered with the
# 503 that says to retry with the same key. A foreign constraint reaching that point is a real
# conflict of the caller's own, and is re-raised instead of dressed up as retry advice.
CREATE_KEY_CONSTRAINT = "uq_memos_owner_idempotency_key"


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
            CREATE_KEY_CONSTRAINT,
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
    # What this code may be redeemed for: a device token ('login') or a management grant
    # ('manage'). It lives on the ROW because the encrypted transaction cookie that carried it
    # through the Authelia round trip is deleted at the callback — after that, the row is the only
    # thing left that can tell the two exchange endpoints' codes apart. The server default is
    # 'login', which is exactly what every code minted before this column existed was.
    purpose: Mapped[str] = mapped_column(String(16), server_default="login")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManagementGrant(Base):
    """A short-lived step-up credential for token management, minted by a fresh Authelia login.

    Modelled on `NativeAuthCode`: only the sha256 is stored, so a database read cannot be replayed
    into a live credential, and rows are swept once expired.

    Unlike a handoff code it carries no `consumed_at` and is deliberately MULTI-USE within its TTL:
    it authenticates a management SESSION of minutes (list, create, revoke), not a single redemption
    — the single-use property belongs to the CODE that was exchanged for it. The bound that keeps
    it safe is time (`MANAGE_GRANT_TTL_S`), not use count.
    """

    __tablename__ = "management_grants"

    id: Mapped[int] = mapped_column(primary_key=True)
    hashed_grant: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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


# --- Daily digest (MEMNG-233) -----------------------------------------------------------------
#
# The digest's state used to live in a satellite's `state.json` plus its `.env`. These five tables
# ARE that state, so the admin panel and the send pipeline read one authority instead of two.


class DigestTrigger(enum.StrEnum):
    """What started a run. A plain string column, for the reason `JobKind` is one: the member set
    grows (this one already gained `import` after the Story was written) and a free string costs no
    `ALTER TYPE`, which cannot run in a transaction alongside its own use."""

    scheduled = "scheduled"
    manual = "manual"
    test = "test"
    # `import` is a Python keyword, so the MEMBER is spelled with a trailing underscore while the
    # VALUE — the only thing the database and the API ever see — is exactly `import`.
    import_ = "import"


class DigestRunStatus(enum.StrEnum):
    """A run's TERMINAL outcome. NULL means "still in flight", which is a state this enum
    deliberately cannot spell: see `DigestRun.status`."""

    sent = "sent"
    nothing_to_send = "nothing_to_send"
    deferred = "deferred"
    failed = "failed"


class DigestVisibilityScope(enum.StrEnum):
    """Which memos a digest may mail. `PUBLIC_PROTECTED` is the default (design 3285 §9)."""

    PUBLIC = "PUBLIC"
    PUBLIC_PROTECTED = "PUBLIC_PROTECTED"


class HostedMediaKind(enum.StrEnum):
    video = "video"
    image_full = "image_full"


class StatusAlreadyTerminal(ValueError):
    """A second write to a `digest_runs.status` that already holds a terminal value."""


class DigestSettings(Base):
    """The ONE row (id=1) that configures the digest.

    A singleton rather than a generic `system_settings(key, value_json)` table, deliberately: the
    twenty fields are typed, defaulted at the migration level and bound straight to a form. What
    makes a second row impossible is two things together — the `CHECK (id = 1)` below, and a router
    that only ever UPDATEs (app/routers/admin_digest.py has no INSERT path). Either alone would be
    a convention; together they are a constraint.

    Every non-nullable column carries a `server_default`, `id` included, so the seeded row can be
    written by naming no column at all and so a column added by a later expand-only migration has
    a defined value for the row that already exists. `updated_by` is the one nullable column: the
    seed was written by the migration, not by a person.
    """

    __tablename__ = "digest_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_digest_settings_singleton"),)

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, server_default=text("1"), default=1
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    send_time: Mapped[time] = mapped_column(
        Time, default=time(7, 0), server_default=text("'07:00'")
    )
    timezone: Mapped[str] = mapped_column(
        String(64), default="Europe/Zurich", server_default="Europe/Zurich"
    )
    visibility_scope: Mapped[str] = mapped_column(
        String(32),
        default=DigestVisibilityScope.PUBLIC_PROTECTED,
        server_default=DigestVisibilityScope.PUBLIC_PROTECTED,
    )
    start_date: Mapped[date] = mapped_column(
        Date, default=date(2026, 6, 1), server_default=text("'2026-06-01'")
    )
    # Minutes after send_time within which a late worker pass may still run today's digest.
    grace_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"))
    max_email_bytes: Mapped[int] = mapped_column(
        Integer, default=18874368, server_default=text("18874368")
    )
    max_videos_per_memo: Mapped[int] = mapped_column(Integer, default=2, server_default=text("2"))
    # Plausibility cap: a wrong scope or start_date must not mail the whole corpus to six external
    # recipients in one morning. A selection above this fails the run instead of sending it.
    max_memos_per_run: Mapped[int] = mapped_column(Integer, default=40, server_default=text("40"))
    mail_title: Mapped[str] = mapped_column(
        String(255),
        default="The Daily Dami Memos Mail",
        server_default="The Daily Dami Memos Mail",
    )
    from_name: Mapped[str] = mapped_column(
        String(255),
        default="The Daily Dami Memos Mail",
        server_default="The Daily Dami Memos Mail",
    )
    # Empty means "bind to SMTP_USERNAME at send time" — the address that actually authenticates.
    from_email: Mapped[str] = mapped_column(String(320), default="", server_default="")
    hosting_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    dropbox_folder: Mapped[str] = mapped_column(
        String(255), default="/memos-mail", server_default="/memos-mail"
    )
    retention_days: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"))
    link_images_full: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))


class DigestRecipient(Base):
    """One mailbox the digest goes to. Recipients are OUTSIDE the household and hold no account
    here, so this is a list of addresses and not a join onto `users`.

    `email` is stored lower-cased and UNIQUE. The lower-casing happens in the router (the only
    writer), which is what makes the plain unique index a case-INSENSITIVE one: two spellings of
    the same address normalise to the same stored string and the second insert conflicts."""

    __tablename__ = "digest_recipients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    added_by: Mapped[str | None] = mapped_column(String(255))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DigestRun(Base):
    """One attempt at a digest — and, while `status` is NULL, the single-flight LOCK itself.

    The runner INSERTs and COMMITs this row before any other work, and two unique partial indexes
    turn that commit into the lock:

    * `uq_digest_runs_scheduled_local_date` — one `scheduled` row per local day (the day ledger).
    * `uq_digest_runs_single_inflight` — `UNIQUE ((1)) WHERE status IS NULL`, a constant-expression
      index, so at most ONE in-flight run of ANY trigger exists at a time. This is what covers a
      manual run, which sits outside the day ledger by design; without it "refuse while an
      in-flight run exists" would be a check-then-insert race that two concurrent callers both win.
    * `uq_digest_runs_import` — the one-shot import owns exactly one row, so a re-run of the import
      updates its aggregates instead of adding a second run.

    A committed row is durable and self-fencing: unlike a session-scoped advisory lock, no
    connection death, reap or crash releases it, so there is nothing to fence against.

    `status` is NULLABLE and written EXACTLY ONCE, terminally, in the same transaction that sets
    `finished_at`. `memosng_digest_runs_total{status}` is a DB-derived Counter and a Counter may
    not move backwards: a row that carried a status before its outcome was known would either have
    to be corrected later (decrementing a label) or would publish a false terminal result. The one
    NULL→terminal write a runner does not make itself is the worker's stale-run sweep, which marks
    a run that outlived its process `failed`; that is still exactly one write.
    """

    __tablename__ = "digest_runs"
    __table_args__ = (
        Index(
            "uq_digest_runs_scheduled_local_date",
            "local_date",
            unique=True,
            postgresql_where=text("trigger = 'scheduled'"),
        ),
        Index(
            "uq_digest_runs_single_inflight",
            text("(1)"),
            unique=True,
            postgresql_where=text("status IS NULL"),
        ),
        Index(
            "uq_digest_runs_import",
            "trigger",
            unique=True,
            postgresql_where=text("trigger = 'import'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(32))
    memos_sent: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    parts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Claim-before-send fencing (user decision 1, per-accepted-part commit): a part is CLAIMED with
    # a CAS on parts_claimed before it touches SMTP, and parts_sent advances after acceptance.
    parts_sent: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    parts_claimed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    recipients: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    local_date: Mapped[date] = mapped_column(Date)

    @validates("status")
    def _status_is_written_once(self, _key: str, value: str | None) -> str | None:
        """Refuse any assignment to a `status` that already holds a terminal value.

        The Counter monotonicity above is a property of the DATA, so it is guarded where the data
        is written rather than in one caller. This guards ORM assignment — the only path Phase 1
        and the pipeline use; a hand-written `UPDATE digest_runs SET status = …` bypasses it, and
        no code in this repo writes that statement.
        """
        if self.status is not None:
            raise StatusAlreadyTerminal(
                f"digest_runs.status is already {self.status!r} and is written exactly once; "
                f"refusing to overwrite it with {value!r}"
            )
        return value


class DigestDelivery(Base):
    """The registry of what has already been mailed: one row per memo, forever. It is what stops a
    memo being sent twice across runs, so `memo_id` is the primary key and not merely indexed.

    `run_id` is NOT NULL and carries NO server default, deliberately. It is a required FK, and a
    default would let an insert that forgot it either fail obscurely on the FK or — worse — attach
    the delivery to an arbitrary run and destroy exactly the audit relationship the column exists
    for. An omitted `run_id` fails loudly at the NOT NULL instead. There is no `ondelete` on it
    either: a run that produced deliveries cannot be deleted out from under them.

    `part_index` records WHICH accepted SMTP part carried this memo — the commit boundary moved to
    per-part (user decision 1), so a run's deliveries land in several transactions and the part
    they belong to is the only way to read that history back.
    """

    __tablename__ = "digest_deliveries"

    memo_id: Mapped[int] = mapped_column(
        ForeignKey("memos.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("digest_runs.id"), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    part_index: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    # The memo went out with some of its media left off (shed to stay under max_email_bytes).
    incomplete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class DigestHostedMedia(Base):
    """A file uploaded to Dropbox for one mail, and the shared link that mail points at — the
    table that replaces the satellite's `state.json["hosted_media"]`.

    `swept_at` is stamped by the retention sweep when the file is deleted from Dropbox; the row is
    KEPT so a later run can tell "never hosted" from "hosted and since swept" (links in older mails
    die, which is today's behaviour and an accepted consequence of retention)."""

    __tablename__ = "digest_hosted_media"

    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    kind: Mapped[str] = mapped_column(String(16))
    dropbox_path: Mapped[str] = mapped_column(Text)
    shared_link: Mapped[str] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    swept_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
