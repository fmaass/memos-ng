"""users.password_hash

Revision ID: f8af654fa068
Revises: 22f3df18f2df
Create Date: 2026-11-05 12:00:00

Expand-only (MEMNG-1097). One NEW nullable column on `users`: the argon2id hash a local password
sign-in (LOCAL_LOGIN=1) is checked against. NULL means the account has no local password, which is
every existing row and every account created by an OIDC login, so nothing is backfilled and the
previous image — whose ORM does not name the column — keeps reading and inserting users unchanged.
With LOCAL_LOGIN unset nothing reads it. The downgrade drops the column and nothing else.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8af654fa068"
down_revision: str | None = "22f3df18f2df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
