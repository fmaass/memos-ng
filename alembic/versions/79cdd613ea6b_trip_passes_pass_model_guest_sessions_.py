"""trip passes: pass model, guest sessions, ingest batches

Revision ID: 79cdd613ea6b
Revises: 294410e70c5f
Create Date: 2026-10-27 19:46:47.275142

Expand-only (trip pass P1). Seven NEW tables — `passes`, `pass_devices`, `pass_events`,
`guest_uploads`, `ingest_batches`, `ingest_blobs`, `pass_sync_state` — and four NULLABLE columns on
existing tables with no default and no backfill: `attachments.contributed_by_device_id` (FK SET
NULL) and `attachments.contributor_name`, `memos.pass_id` (FK SET NULL) and `memos.guest_caption`.
Adding a nullable column without a default is a catalogue change; no existing row is rewritten and
no existing column changes. `uploads` is untouched: guest sessions get their own table.

The previous image's ORM names none of it, so it keeps working against the new schema.
Production never downgrades; the downgrade exists for the rehearsal on a throwaway restore, and it
REFUSES while any pass exists rather than deleting one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "79cdd613ea6b"
down_revision: str | None = "294410e70c5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "passes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.String(length=64), nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=True),
        sa.Column("secret_hash", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=16), server_default="contribute", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_devices", sa.Integer(), server_default="30", nullable=False),
        sa.Column("byte_cap", sa.BigInteger(), server_default="53687091200", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_version", sa.Integer(), nullable=True),
        sa.Column("revoked_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("scope IN ('contribute', 'contribute_view')", name="ck_passes_scope"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_version IS NULL)", name="ck_passes_revoked_pair"
        ),
        sa.CheckConstraint("max_devices > 0", name="ck_passes_max_devices"),
        sa.CheckConstraint("version > 0", name="ck_passes_version"),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash"),
    )
    op.create_index(op.f("ix_passes_collection_id"), "passes", ["collection_id"], unique=False)
    op.create_index(op.f("ix_passes_uid"), "passes", ["uid"], unique=True)
    op.create_table(
        "pass_devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.String(length=64), nullable=False),
        sa.Column("pass_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=True),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=8), nullable=False),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploads_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_total", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_version", sa.Integer(), nullable=True),
        sa.CheckConstraint("platform IN ('web', 'clip', 'app')", name="ck_pass_devices_platform"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_version IS NULL)", name="ck_pass_devices_revoked_pair"
        ),
        sa.ForeignKeyConstraint(["pass_id"], ["passes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_pass_devices_pass_id"), "pass_devices", ["pass_id"], unique=False)
    op.create_index(op.f("ix_pass_devices_uid"), "pass_devices", ["uid"], unique=True)
    op.create_table(
        "guest_uploads",
        sa.Column("upload_uid", sa.String(length=64), nullable=False),
        sa.Column("pass_device_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("mime", sa.String(length=255), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("bytes_received", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("client_mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["pass_device_id"], ["pass_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("upload_uid"),
    )
    op.create_index(
        op.f("ix_guest_uploads_pass_device_id"), "guest_uploads", ["pass_device_id"], unique=False
    )
    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.String(length=64), nullable=False),
        sa.Column("pass_id", sa.Integer(), nullable=False),
        sa.Column("pass_device_id", sa.Integer(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("byte_total", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("memo_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('open', 'complete', 'acked', 'quarantined')", name="ck_ingest_batches_state"
        ),
        sa.ForeignKeyConstraint(["memo_id"], ["memos.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pass_device_id"], ["pass_devices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pass_id"], ["passes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ingest_batches_pass_id"), "ingest_batches", ["pass_id"], unique=False)
    op.create_index(op.f("ix_ingest_batches_state"), "ingest_batches", ["state"], unique=False)
    op.create_index(op.f("ix_ingest_batches_uid"), "ingest_batches", ["uid"], unique=True)
    op.create_table(
        "pass_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("pass_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('issued', 'updated', 'revoked', 'device_revoked', 'redeemed', 'named', "
            "'upload_started', 'upload_finalized', 'refused', 'hidden', 'unhidden', 'removed')",
            name="ck_pass_events_kind",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["device_id"], ["pass_devices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pass_id"], ["passes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pass_events_pass_id"), "pass_events", ["pass_id"], unique=False)
    op.create_table(
        "pass_sync_state",
        sa.Column("pass_id", sa.Integer(), nullable=False),
        sa.Column("acked_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("push_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_urgent", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pull_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["pass_id"], ["passes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("pass_id"),
    )
    op.create_table(
        "ingest_blobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(length=255), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column("attachment_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["batch_id"], ["ingest_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "sha256", name="uq_ingest_blobs_batch_sha256"),
    )
    op.create_index(op.f("ix_ingest_blobs_batch_id"), "ingest_blobs", ["batch_id"], unique=False)
    op.create_index(op.f("ix_ingest_blobs_sha256"), "ingest_blobs", ["sha256"], unique=False)
    op.add_column("attachments", sa.Column("contributed_by_device_id", sa.Integer(), nullable=True))
    op.add_column("attachments", sa.Column("contributor_name", sa.String(length=64), nullable=True))
    op.create_index(
        op.f("ix_attachments_contributed_by_device_id"),
        "attachments",
        ["contributed_by_device_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_attachments_contributed_by_device_id",
        "attachments",
        "pass_devices",
        ["contributed_by_device_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("memos", sa.Column("pass_id", sa.Integer(), nullable=True))
    op.add_column("memos", sa.Column("guest_caption", sa.Text(), nullable=True))
    op.create_index(op.f("ix_memos_pass_id"), "memos", ["pass_id"], unique=False)
    op.create_foreign_key(
        "fk_memos_pass_id", "memos", "passes", ["pass_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    passes = op.get_bind().execute(sa.text("SELECT count(*) FROM passes"))
    if passes.scalar_one():
        raise RuntimeError("refusing to downgrade: a trip pass exists")
    op.drop_constraint("fk_memos_pass_id", "memos", type_="foreignkey")
    op.drop_index(op.f("ix_memos_pass_id"), table_name="memos")
    op.drop_column("memos", "guest_caption")
    op.drop_column("memos", "pass_id")
    op.drop_constraint("fk_attachments_contributed_by_device_id", "attachments", type_="foreignkey")
    op.drop_index(op.f("ix_attachments_contributed_by_device_id"), table_name="attachments")
    op.drop_column("attachments", "contributor_name")
    op.drop_column("attachments", "contributed_by_device_id")
    op.drop_index(op.f("ix_ingest_blobs_sha256"), table_name="ingest_blobs")
    op.drop_index(op.f("ix_ingest_blobs_batch_id"), table_name="ingest_blobs")
    op.drop_table("ingest_blobs")
    op.drop_table("pass_sync_state")
    op.drop_index(op.f("ix_pass_events_pass_id"), table_name="pass_events")
    op.drop_table("pass_events")
    op.drop_index(op.f("ix_ingest_batches_uid"), table_name="ingest_batches")
    op.drop_index(op.f("ix_ingest_batches_state"), table_name="ingest_batches")
    op.drop_index(op.f("ix_ingest_batches_pass_id"), table_name="ingest_batches")
    op.drop_table("ingest_batches")
    op.drop_index(op.f("ix_guest_uploads_pass_device_id"), table_name="guest_uploads")
    op.drop_table("guest_uploads")
    op.drop_index(op.f("ix_pass_devices_uid"), table_name="pass_devices")
    op.drop_index(op.f("ix_pass_devices_pass_id"), table_name="pass_devices")
    op.drop_table("pass_devices")
    op.drop_index(op.f("ix_passes_uid"), table_name="passes")
    op.drop_index(op.f("ix_passes_collection_id"), table_name="passes")
    op.drop_table("passes")
