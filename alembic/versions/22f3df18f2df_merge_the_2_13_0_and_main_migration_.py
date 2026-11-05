"""Merge the 2.13.0 and main migration heads

Revision ID: 22f3df18f2df
Revises: a13d0e5c7b21, c4e1a7d93b52
Create Date: 2026-11-05 10:44:29.585348

"""

from collections.abc import Sequence

revision: str = "22f3df18f2df"
down_revision: str | Sequence[str] | None = ("a13d0e5c7b21", "c4e1a7d93b52")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
