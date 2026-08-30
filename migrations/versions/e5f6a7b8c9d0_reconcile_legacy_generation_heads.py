"""Reconcile the legacy generation merge with the current migration head.

The ``c3...`` branch was deployed by an intermediate build that removed the
generation failover columns and circuit-state table.  The current runtime
still reads those fields, so this merge repairs that schema while remaining
safe for databases that already have the objects from ``e4...``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = (
    "c3d4e5f6a7b8",
    "d4e9f2a1b7c3",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def existing_columns(table_name: str) -> set[str]:
        return {column["name"] for column in inspector.get_columns(table_name)}

    generation_item_columns = existing_columns("generation_items")
    for name, column in (
        (
            "attempted_channel_ids",
            sa.Column(
                "attempted_channel_ids",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                server_default=sa.text("'[]'"),
                nullable=False,
            ),
        ),
        (
            "circuit_probe",
            sa.Column(
                "circuit_probe",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        ),
    ):
        if name in generation_item_columns:
            continue
        with op.batch_alter_table("generation_items", schema=None) as batch_op:
            batch_op.add_column(column)
        generation_item_columns.add(name)

    generation_attempt_columns = existing_columns("generation_attempts")
    if "circuit_probe" not in generation_attempt_columns:
        with op.batch_alter_table("generation_attempts", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "circuit_probe",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )

    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "channel_circuit_states" not in table_names:
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
    elif "ix_channel_circuit_states_open_until" not in {
        index["name"] for index in inspector.get_indexes("channel_circuit_states")
    }:
        op.create_index(
            "ix_channel_circuit_states_open_until",
            "channel_circuit_states",
            ["open_until"],
            unique=False,
        )


def downgrade() -> None:
    # The repaired objects may contain live retry state.  Keep them when
    # downgrading this compatibility merge; the owning historical revision
    # remains responsible for destructive changes.
    pass
