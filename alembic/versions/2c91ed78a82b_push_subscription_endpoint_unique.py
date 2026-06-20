"""push_subscription endpoint unique

Revision ID: 2c91ed78a82b
Revises: 02b3c6b590f7
Create Date: 2026-06-20 11:54:08.598590

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '2c91ed78a82b'
down_revision: str | None = '02b3c6b590f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_push_subscriptions_endpoint", "push_subscriptions", ["endpoint"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_push_subscriptions_endpoint", "push_subscriptions", type_="unique")
