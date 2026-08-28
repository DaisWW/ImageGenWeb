"""Persist background-removal adapter and option snapshots.

Revision ID: c2d7e9f1a4b6
Revises: b8e4c2d7f1a6
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c2d7e9f1a4b6"
down_revision: Union[str, Sequence[str], None] = "b8e4c2d7f1a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("background_removal_results")
    }


def upgrade() -> None:
    existing = _existing_columns()
    with op.batch_alter_table("background_removal_results", schema=None) as batch_op:
        if "adapter_id" not in existing:
            batch_op.add_column(
                sa.Column(
                    "adapter_id",
                    sa.String(length=40),
                    server_default="lucida",
                    nullable=False,
                )
            )
        if "adapter_options" not in existing:
            batch_op.add_column(
                sa.Column(
                    "adapter_options",
                    sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                    server_default=sa.text("'{}'"),
                    nullable=False,
                )
            )


def downgrade() -> None:
    existing = _existing_columns()
    with op.batch_alter_table("background_removal_results", schema=None) as batch_op:
        if "adapter_options" in existing:
            batch_op.drop_column("adapter_options")
        if "adapter_id" in existing:
            batch_op.drop_column("adapter_id")
