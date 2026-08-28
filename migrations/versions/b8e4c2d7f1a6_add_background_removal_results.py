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


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _create_index_if_missing(
    name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    sqlite_where=None,
    postgresql_where=None,
) -> None:
    if name in _index_names(table_name):
        return
    kwargs = {"unique": unique}
    if sqlite_where is not None:
        kwargs["sqlite_where"] = sqlite_where
    if postgresql_where is not None:
        kwargs["postgresql_where"] = postgresql_where
    op.create_index(name, table_name, columns, **kwargs)


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    # ``AUTO_CREATE_DB`` can create the current model metadata before Alembic
    # runs in local development.  Treat those tables as an existing baseline
    # and only add the indexes that are still missing.
    if "background_removal_runs" not in _table_names():
        op.create_table(
            "background_removal_runs",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("source_item_id", sa.String(length=32), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["source_item_id"], ["generation_items.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_item_id", name="uq_background_removal_run_source_item"),
        )
    for name, columns in (
        ("ix_background_removal_runs_source_item_id", ["source_item_id"]),
        ("ix_background_removal_runs_status", ["status"]),
        ("ix_background_removal_runs_user_id", ["user_id"]),
        ("ix_background_removal_runs_user_updated", ["user_id", "updated_at"]),
    ):
        _create_index_if_missing(name, "background_removal_runs", columns)

    if "background_removal_results" not in _table_names():
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
            sa.ForeignKeyConstraint(
                ["run_id"], ["background_removal_runs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("output_path"),
            sa.UniqueConstraint("thumbnail_path"),
            sa.UniqueConstraint("run_id", "model_id", name="uq_background_removal_run_model"),
        )
    for name, columns in (
        ("ix_background_removal_results_model_id", ["model_id"]),
        ("ix_background_removal_results_model_status", ["model_id", "status"]),
        ("ix_background_removal_results_queue", ["status", "created_at"]),
        ("ix_background_removal_results_run_id", ["run_id"]),
        ("ix_background_removal_results_status", ["status"]),
    ):
        _create_index_if_missing(name, "background_removal_results", columns)
    _create_index_if_missing(
        "uq_background_removal_results_run_selected",
        "background_removal_results",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("selected = 1"),
        postgresql_where=sa.text("selected"),
    )


def downgrade() -> None:
    if "background_removal_results" in _table_names():
        for name in (
            "uq_background_removal_results_run_selected",
            "ix_background_removal_results_status",
            "ix_background_removal_results_run_id",
            "ix_background_removal_results_queue",
            "ix_background_removal_results_model_status",
            "ix_background_removal_results_model_id",
        ):
            _drop_index_if_exists(name, "background_removal_results")
        op.drop_table("background_removal_results")
    if "background_removal_runs" in _table_names():
        for name in (
            "ix_background_removal_runs_user_updated",
            "ix_background_removal_runs_user_id",
            "ix_background_removal_runs_status",
            "ix_background_removal_runs_source_item_id",
        ):
            _drop_index_if_exists(name, "background_removal_runs")
        op.drop_table("background_removal_runs")
