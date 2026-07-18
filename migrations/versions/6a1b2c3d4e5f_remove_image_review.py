"""Remove the retired image review result column.

Revision ID: 6a1b2c3d4e5f
Revises: 4f2a8c1d9b70
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6a1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "4f2a8c1d9b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def upgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("review")


def downgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("review", JSON_TYPE, nullable=False, server_default=sa.text("'{}'"))
        )
