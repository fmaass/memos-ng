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
    # position first, NULLS LAST, then id: the saved order IS the order the composer sent
    # (MEMNG-262). Every row written before that column existed carries NULL and therefore keeps
    # its historical id order, behind anything positioned — which is why an edit that adds a file
    # to such a memo renumbers the whole memo first (services.memos.link_attachments).
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment",
        lazy="selectin",
        order_by="(Attachment.position.nulls_last(), Attachment.id)",
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
    # Original capture time (EXIF/QuickTime → File.lastModified → upload time). The first link of
    # that chain is `capture_instant` below, copied here at finalize — before MEMNG-262 nothing
    # read a file's metadata and the chain started at the browser's lastModified.
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Capture-only facts, read from the file's OWN metadata at upload completion and never
    # inferred (MEMNG-262). NULL means the file said nothing — which is different from
    # `captured_at`, which always holds something. `capture_civil` is a wall-clock reading whose
    # zone the file never recorded (EXIF without OffsetTimeOriginal); only a client knows the zone
    # to resolve it in, so it travels unresolved. `capture_source` is 'exif' or 'quicktime'.
    capture_instant: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_civil: Mapped[str | None] = mapped_column(String(32))
    capture_source: Mapped[str | None] = mapped_column(String(16))
    # 0-based place in this memo's attachment order, written at finalize from the order the client
    # declared (`upload_uids`). NULL on every row that predates the column, and on an attachment
    # that belongs to no memo.
    position: Mapped[int | None] = mapped_column(Integer)
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
    # The capture read of these bytes (MEMNG-262), taken ONCE when the last chunk lands and
    # carried onto the Attachment at finalize. `capture_state` is what
    # `GET /api/uploads/{uid}/capture` answers with: 'pending' until the read has happened,
    # 'done' afterwards — including when the file simply had no date, which is a settled answer
    # and not a reason for a composer to keep waiting.
    capture_state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    capture_instant: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_civil: Mapped[str | None] = mapped_column(String(32))
    capture_source: Mapped[str | None] = mapped_column(String(16))
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
    # WHICH print profile this job was asked for (MEMNG-238), snapshot at enqueue. NULL means the
    # default profile — every printready job written before this column existed produced the
    # shipped 4096/q92 tier at the legacy path, so NULL and `print-4096` are the same job. Only
    # `kind = printready` ever sets it; the upload pipeline has no profile.
    profile_key: Mapped[str | None] = mapped_column(String(64))
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
    # `test` names a trigger no ROW ever carries: a test-send persists nothing — no run row, no
    # hosted media, no delivery — because its job is to prove the SMTP channel, not to consume the
    # day's ledger slot. It stays in the vocabulary because the metrics snapshot excludes it from
    # "a run that delivered" by NAME (`NON_DELIVERING_TRIGGERS`), and a policy that excludes a
    # value the enum cannot spell is a policy nothing can check.
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


class HostedMediaStatus(enum.StrEnum):
    """The lifecycle of ONE external object, and the only three states it can be in.

    `pending` is written BEFORE the upload starts, so an object that reaches Dropbox always has a
    row naming it; `uploaded` is written immediately after that one upload succeeds; `swept` is
    written only after the remote delete has actually succeeded. Transitions are one-way and are
    enforced by a CAS predicate in the statement (`WHERE status = :expected`), never by reading
    first — the same rule `digest_runs.status` follows and for the same reason.
    """

    pending = "pending"
    uploaded = "uploaded"
    swept = "swept"


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
    # MEDIA-readiness grace, not a scheduling window: how long selection waits for a memo's
    # derived tiers (thumb, web mp4) before giving up on them. Inside the grace the memo is
    # deferred and the scan STOPS there, so a later memo can never overtake an earlier one; past
    # the grace it goes out flagged `incomplete`. The scheduling window is a different thing
    # entirely and is not configurable — it runs from `send_time` to local midnight.
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


class PrintMetadataPolicy(enum.StrEnum):
    """What of the source's metadata survives into a print rendition.

    `strip` is what the shipped encoder does by writing no EXIF at all. `keep_dates` REBUILDS a
    whitelist of date tags into fresh EXIF rather than passing the source's through — a
    pass-through would carry GPS coordinates, camera serial numbers and a stale Orientation (which
    is already baked into the pixels) into a file that leaves the household for a print service.
    Neither policy touches the ICC profile or the baked orientation: those are print correctness,
    not metadata.
    """

    # UPPERCASE members, lowercase values: `strip` as a member name would shadow `str.strip` on a
    # StrEnum, which is both a type error and a trap for anything that calls a string method on a
    # policy value. The stored and published value is the lowercase one.
    STRIP = "strip"
    KEEP_DATES = "keep_dates"


class MailSubsampling(enum.StrEnum):
    """Chroma subsampling for the mail profile, in the notation the vendor guidance uses.

    `4:2:0` is the mail default and is deliberately NOT what print uses: an inline mail image is
    read at a few hundred pixels on a phone, where the halved chroma resolution is invisible and
    the bytes it saves are the difference between a message that arrives and one that bounces off
    a provider's part cap. `4:4:4` is the only alternative offered — an escape hatch for an image
    whose fine coloured detail the subsampling visibly eats. There is no `4:2:2`: it is a
    compromise between the two that saves little and is not worth a third answer in a panel.
    """

    C420 = "4:2:0"
    C444 = "4:4:4"


class ExportSettings(Base):
    """The ONE row (id=1) holding the two named image profiles every export resolves from.

    Same shape and the same two guarantees as `DigestSettings`: `CHECK (id = 1)` in the database
    and a router with no INSERT path (app/routers/admin_export.py). Every non-nullable column
    carries a `server_default`, so the migration seeds the row by naming only `id` and a column
    added by a later expand-only migration has a defined value for the row that already exists.

    Two profiles, one row, because they are one decision: how this deployment encodes an image for
    somewhere that is not a screen. The `print_*` group is the photo-book tier's (MEMNG-49) and its
    defaults are exactly what that tier already ships, so the seeded row resolves to the existing
    `print-4096` profile and changes nothing on disk. The `mail_*` group is stored and exposed
    here and consumed by the digest renderer in its own story — nothing reads it yet, which is why
    it has no bearing on any cache key.
    """

    __tablename__ = "export_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_export_settings_singleton"),)

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, server_default=text("1"), default=1
    )
    # 4096 (the shipped rung), 5000 (Blurb's published full-bleed rule) or 0 = the source's own
    # size. Not an enum type in the database: the admissible set is a request-validation question
    # and a migration is the wrong place to relitigate a print-size decision.
    print_long_edge: Mapped[int] = mapped_column(Integer, default=4096, server_default=text("4096"))
    print_jpeg_quality: Mapped[int] = mapped_column(Integer, default=92, server_default=text("92"))
    print_metadata_policy: Mapped[str] = mapped_column(
        String(16),
        default=PrintMetadataPolicy.STRIP,
        server_default=PrintMetadataPolicy.STRIP,
    )
    print_grayscale_single_channel: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    mail_long_edge: Mapped[int] = mapped_column(Integer, default=1600, server_default=text("1600"))
    mail_jpeg_quality: Mapped[int] = mapped_column(Integer, default=80, server_default=text("80"))
    mail_subsampling: Mapped[str] = mapped_column(
        String(8), default=MailSubsampling.C420, server_default=MailSubsampling.C420
    )
    mail_progressive: Mapped[bool] = mapped_column(
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

    Two writers is also why the once-only rule cannot live in the ORM alone. The pipeline's own
    claims, progress updates and terminal write are hand-written UPDATEs carrying `AND status IS
    NULL` in the SQL (`app/services/digest/runner.py`), because a validator on a mapped instance
    guards ONE session's object and knows nothing about the sweep committing from another
    connection. The predicate in the statement is the real guard; the validator below catches the
    in-session second assignment the predicate alone would let through inside one uncommitted
    transaction.
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
    # LIVENESS, which is what the stale-run sweep must test — never elapsed time. A legitimate run
    # can exceed two hours (up to `max_memos_per_run` memos, several videos each, Dropbox upload
    # sessions that allow 600 s PER OPERATION), so a sweep measuring from `started_at` would
    # declare a healthy run dead, drop it out of `uq_digest_runs_single_inflight`, and let a second
    # run send the same memos beside it. The runner stamps this before and after every part send
    # and after every Dropbox upload-session operation, so the longest gap a healthy run leaves is
    # one operation; the sweep's bar is 15 minutes of silence. NULL means "has not beaten yet", and
    # the sweep judges such a run on `started_at` (COALESCE) at the same bar.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("status")
    def _status_is_written_once(self, _key: str, value: str | None) -> str | None:
        """Refuse any assignment to a `status` that already holds a terminal value.

        The Counter monotonicity above is a property of the DATA, so it is guarded where the data
        is written rather than in one caller. This guards ORM assignment — the admin API's and the
        import's path. It is NOT the only guard and must not be read as one: the pipeline writes
        its terminal status as a fenced `UPDATE … WHERE id = :id AND status IS NULL`, which loads
        no instance and so never reaches this validator. Cross-session protection is that
        predicate; this is what catches a second assignment inside one uncommitted transaction,
        which the predicate alone would let through.
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
    """The OWNERSHIP LEDGER for every file the digest puts on Dropbox — one row per remote object.

    It is a ledger of an EXTERNAL resource, not a cache keyed by an attachment, and three
    properties follow from that and are the reason the table has this shape rather than its first
    one (`attachment_id` as the primary key, written in one batch after the whole hosting pass):

    * **The row is written BEFORE the upload, not after it.** The path a file will occupy is
      deterministic, so it can be committed as `pending` first. A crash between Dropbox accepting
      a file and this process writing anything would otherwise leave an object that no row names
      and no sweep can ever find — an orphan nothing can clean up, because the only locator lived
      in the memory of a process that died.
    * **The row OUTLIVES the attachment.** `attachment_id` is nullable and its FK is
      `ON DELETE SET NULL`, never CASCADE: Dropbox cannot participate in a database cascade, so
      deleting the row when the attachment is reclaimed deletes the sole locator of a file that is
      still there. The tombstone survives, `attachment_id` goes NULL, and the retention sweep
      still deletes the object it names.
    * **`id` is a surrogate key.** One attachment can be hosted more than once (a later run writes
      a new run folder, so a new path), and each remote object needs its own row. `dropbox_path`
      is the natural key of the OBJECT and is UNIQUE, which is what makes committing the intention
      idempotent across a retry.

    `swept_at` is stamped only after the remote delete has actually succeeded — it is the claim
    that the file IS gone, and stamping it for a failed delete would leave a live file no sweep
    ever looks at again. A `pending` row is judged by probing the remote path — present means the
    upload landed and the process died before it could say so, absent means it never landed — but
    ONLY once its owning run is provably not alive: an upload-session operation may legitimately
    run for ten minutes and a whole run for hours, and reconciling a row out from under a live
    upload is how the sweep would invent the very orphan it exists to prevent.
    """

    __tablename__ = "digest_hosted_media"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'uploaded', 'swept')",
            name="ck_digest_hosted_media_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULLABLE and ON DELETE SET NULL. See the class docstring: a cascade here deletes the only
    # locator of a file that still exists on Dropbox.
    attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), index=True
    )
    # WHICH run committed this intention — the liveness the reconciliation sweep judges a
    # `pending` row by. Without it "stuck" can only be measured as elapsed time, and elapsed time
    # cannot tell a dead process from a slow upload. Nullable and `ON DELETE SET NULL` for the
    # same reason `attachment_id` is: the tombstone must outlive everything it references.
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("digest_runs.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    # The natural key of the remote OBJECT, committed before the upload runs. Unique, so a retry
    # of the same intention updates one row instead of adding a second claim on one file.
    dropbox_path: Mapped[str] = mapped_column(Text, unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=HostedMediaStatus.pending, server_default=HostedMediaStatus.pending
    )
    # NULL until the upload succeeds: a link is what an `uploaded` row promises.
    shared_link: Mapped[str | None] = mapped_column(Text)
    # When the INTENTION was committed. The reconciliation threshold for a stuck `pending` row is
    # measured from here, because such a row has no `uploaded_at` to be measured from.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # NULL until the upload succeeds. Retention age is measured from it, so a pending row is never
    # swept on age alone — it is reconciled against the remote first.
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    swept_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
