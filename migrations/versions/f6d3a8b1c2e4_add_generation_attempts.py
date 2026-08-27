"""Persist generation provider attempts.

Revision ID: f6d3a8b1c2e4
Revises: e4b7c9d2a6f1
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6d3a8b1c2e4"
down_revision: Union[str, Sequence[str], None] = "e4b7c9d2a6f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generation_attempts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("item_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("channel_label", sa.String(length=100), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="running", nullable=False),
        sa.Column("circuit_probe", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("provider_completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("capacity_expires_at", sa.DateTime(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("upstream_status", sa.Integer(), nullable=True),
        sa.Column("upstream_request_id", sa.String(length=255), nullable=True),
        sa.Column("elapsed_seconds", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.ForeignKeyConstraint(["item_id"], ["generation_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_generation_attempt_idempotency_key"),
        sa.UniqueConstraint("item_id", "attempt_number", name="uq_generation_attempt_number"),
    )
    op.create_index(
        "ix_generation_attempts_capacity",
        "generation_attempts",
        ["status", "capacity_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_generation_attempts_channel_status",
        "generation_attempts",
        ["channel_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_generation_attempts_item_id",
        "generation_attempts",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_attempts_user_status",
        "generation_attempts",
        ["user_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generation_attempts_user_status", table_name="generation_attempts")
    op.drop_index("ix_generation_attempts_item_id", table_name="generation_attempts")
    op.drop_index("ix_generation_attempts_channel_status", table_name="generation_attempts")
    op.drop_index("ix_generation_attempts_capacity", table_name="generation_attempts")
    op.drop_table("generation_attempts")
