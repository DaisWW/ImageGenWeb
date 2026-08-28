"""Remove automatic generation failover state.

Revision ID: a8c6e2f1d4b0
Revises: f6d3a8b1c2e4
Create Date: 2026-08-28 00:00:00.000000
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
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    for table_name, column_names in (
        ("generation_items", ("attempted_channel_ids", "circuit_probe")),
        ("generation_attempts", ("circuit_probe",)),
    ):
        existing = _existing_columns(table_name)
        columns_to_drop = [name for name in column_names if name in existing]
        if not columns_to_drop:
            continue
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            for column_name in columns_to_drop:
                batch_op.drop_column(column_name)

    inspector = sa.inspect(op.get_bind())
    if "channel_circuit_states" in inspector.get_table_names():
        indexes = {index["name"] for index in inspector.get_indexes("channel_circuit_states")}
        if "ix_channel_circuit_states_open_until" in indexes:
            op.drop_index(
                "ix_channel_circuit_states_open_until",
                table_name="channel_circuit_states",
            )
        op.drop_table("channel_circuit_states")


def downgrade() -> None:
    if "attempted_channel_ids" not in _existing_columns("generation_items"):
        with op.batch_alter_table("generation_items", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "attempted_channel_ids",
                    sa.JSON().with_variant(
                        postgresql.JSONB(astext_type=sa.Text()),
                        "postgresql",
                    ),
                    server_default=sa.text("'[]'"),
                    nullable=False,
                )
            )
    if "circuit_probe" not in _existing_columns("generation_items"):
        with op.batch_alter_table("generation_items", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "circuit_probe",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )
    if "circuit_probe" not in _existing_columns("generation_attempts"):
        with op.batch_alter_table("generation_attempts", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "circuit_probe",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )

    if "channel_circuit_states" not in sa.inspect(op.get_bind()).get_table_names():
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
