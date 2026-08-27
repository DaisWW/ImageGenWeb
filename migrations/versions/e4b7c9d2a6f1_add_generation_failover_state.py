"""Add generation failover and channel circuit state.

Revision ID: e4b7c9d2a6f1
Revises: c8f4a1b2d3e5
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4b7c9d2a6f1"
down_revision: Union[str, Sequence[str], None] = "c8f4a1b2d3e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "attempted_channel_ids",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "circuit_probe",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
    op.create_table(
        "channel_circuit_states",
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_window_started_at", sa.DateTime(), nullable=True),
        sa.Column("open_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("channel_id"),
    )
    op.create_index(
        "ix_channel_circuit_states_open_until",
        "channel_circuit_states",
        ["open_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("channel_circuit_states")
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("circuit_probe")
        batch_op.drop_column("attempted_channel_ids")
