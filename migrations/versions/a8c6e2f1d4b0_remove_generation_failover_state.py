"""Keep generation failover state compatible with the runtime schema.

Revision ID: a8c6e2f1d4b0
Revises: f6d3a8b1c2e4
Create Date: 2026-08-28 00:00:00.000000

This revision name came from an intermediate branch that removed automatic
channel failover.  The application runtime still relies on that state, so the
merge migration must never drop these columns or the circuit-state table.  The
upgrade below is deliberately idempotent: it repairs a partially-created
schema while leaving the original ``e4...`` objects and their data intact.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8c6e2f1d4b0"
down_revision: Union[str, Sequence[str], None] = "f6d3a8b1c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    # ``e4...`` normally creates all of these objects.  Keep the checks for
    # databases upgraded from the short-lived removal branch or interrupted
    # during deployment.
    for table_name, column_name, column in (
        (
            "generation_items",
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
            "generation_items",
            "circuit_probe",
            sa.Column(
                "circuit_probe",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        ),
        (
            "generation_attempts",
            "circuit_probe",
            sa.Column(
                "circuit_probe",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        ),
    ):
        if column_name in _existing_columns(table_name):
            continue
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.add_column(column)

    inspector = sa.inspect(op.get_bind())
    if "channel_circuit_states" not in inspector.get_table_names():
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
    # The failover objects are owned by ``e4...``.  This compatibility
    # revision does not own them, so downgrading it must not remove live
    # runtime state.
    pass
