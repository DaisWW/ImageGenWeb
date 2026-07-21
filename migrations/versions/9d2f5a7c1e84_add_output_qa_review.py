"""Add persisted output QA reviews.

Revision ID: 9d2f5a7c1e84
Revises: 7c4e2a9b1d63
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9d2f5a7c1e84"
down_revision: Union[str, Sequence[str], None] = "7c4e2a9b1d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def upgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("review", JSON_TYPE, nullable=False, server_default=sa.text("'{}'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("review")
