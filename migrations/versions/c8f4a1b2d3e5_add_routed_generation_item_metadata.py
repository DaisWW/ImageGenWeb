"""Persist the provider selected for each routed generation item.

Revision ID: c8f4a1b2d3e5
Revises: a1b2c3d4e5f6
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8f4a1b2d3e5"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("channel_label", sa.String(length=100), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "provider_price_rmb",
                sa.Numeric(precision=14, scale=4),
                nullable=False,
                server_default="0",
            )
        )
    # Existing items were already tied to a concrete job-level channel.  Carry
    # that metadata forward so historical admin records remain attributable
    # after the new item-level fields are introduced.
    op.execute(
        sa.text(
            "UPDATE generation_items "
            "SET channel_label = COALESCE((SELECT channel_label FROM generation_jobs "
            "WHERE generation_jobs.id = generation_items.job_id), ''), "
            "provider_price_rmb = COALESCE((SELECT price_per_image_rmb FROM generation_jobs "
            "WHERE generation_jobs.id = generation_items.job_id), 0)"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("provider_price_rmb")
        batch_op.drop_column("channel_label")
