"""Add background-removal comparison results.

Revision ID: b8e4c2d7f1a6
Revises: f6d3a8b1c2e4
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4c2d7f1a6"
down_revision: Union[str, Sequence[str], None] = "f6d3a8b1c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "background_removal_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_item_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_item_id"], ["generation_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_item_id", name="uq_background_removal_run_source_item"),
    )
    op.create_index(
        "ix_background_removal_runs_source_item_id",
        "background_removal_runs",
        ["source_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_runs_status",
        "background_removal_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_runs_user_id",
        "background_removal_runs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_runs_user_updated",
        "background_removal_runs",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "background_removal_results",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("model_label", sa.String(length=100), nullable=False),
        sa.Column("model_config_version", sa.String(length=64), nullable=False),
        sa.Column("model_base_url", sa.String(length=500), nullable=False),
        sa.Column("upstream_model", sa.String(length=150), nullable=False),
        sa.Column("model_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("model_max_concurrency", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("selected", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("elapsed_seconds", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("output_path", sa.String(length=500), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=500), nullable=True),
        sa.Column("output_mime_type", sa.String(length=50), nullable=True),
        sa.Column("output_byte_count", sa.Integer(), nullable=True),
        sa.Column("output_width", sa.Integer(), nullable=True),
        sa.Column("output_height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["background_removal_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("output_path"),
        sa.UniqueConstraint("thumbnail_path"),
        sa.UniqueConstraint("run_id", "model_id", name="uq_background_removal_run_model"),
    )
    op.create_index(
        "ix_background_removal_results_model_id",
        "background_removal_results",
        ["model_id"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_results_model_status",
        "background_removal_results",
        ["model_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_results_queue",
        "background_removal_results",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_results_run_id",
        "background_removal_results",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_background_removal_results_status",
        "background_removal_results",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_background_removal_results_run_selected",
        "background_removal_results",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("selected = 1"),
        postgresql_where=sa.text("selected"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_background_removal_results_run_selected",
        table_name="background_removal_results",
    )
    op.drop_index("ix_background_removal_results_status", table_name="background_removal_results")
    op.drop_index("ix_background_removal_results_run_id", table_name="background_removal_results")
    op.drop_index("ix_background_removal_results_queue", table_name="background_removal_results")
    op.drop_index(
        "ix_background_removal_results_model_status",
        table_name="background_removal_results",
    )
    op.drop_index("ix_background_removal_results_model_id", table_name="background_removal_results")
    op.drop_table("background_removal_results")
    op.drop_index("ix_background_removal_runs_user_updated", table_name="background_removal_runs")
    op.drop_index("ix_background_removal_runs_user_id", table_name="background_removal_runs")
    op.drop_index("ix_background_removal_runs_status", table_name="background_removal_runs")
    op.drop_index(
        "ix_background_removal_runs_source_item_id",
        table_name="background_removal_runs",
    )
    op.drop_table("background_removal_runs")
