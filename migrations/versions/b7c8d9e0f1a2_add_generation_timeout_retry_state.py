"""Persist bounded timeout retry state for generation items.

Revision ID: b7c8d9e0f1a2
Revises: f7a8b9c0d1e2
Create Date: 2026-09-03 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_generation_jobs_workspace_active"
RETRY_INDEX_NAME = "ix_generation_items_retry"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {str(index["name"]) for index in inspector.get_indexes(table_name) if index.get("name")}


def _existing_columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)


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


def _workspace_active_index(*, reconnecting: bool) -> None:
    status_values = "'queued', 'running', 'canceling'"
    if reconnecting:
        status_values += ", 'reconnecting'"
    predicate = sa.text(f"status IN ({status_values})")
    _create_index_if_missing(
        INDEX_NAME,
        "generation_jobs",
        ["workspace_id"],
        unique=True,
        sqlite_where=predicate,
        postgresql_where=predicate,
    )


def upgrade() -> None:
    if "generation_jobs" in _table_names():
        _drop_index_if_exists(INDEX_NAME, "generation_jobs")
        _workspace_active_index(reconnecting=True)

    existing_columns = _existing_columns("generation_items")
    missing_columns = [
        column
        for column in (
            sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("retry_limit", sa.Integer(), server_default="5", nullable=False),
            sa.Column("retry_at", sa.DateTime(), nullable=True),
        )
        if column.name not in existing_columns
    ]
    if missing_columns:
        with op.batch_alter_table("generation_items", schema=None) as batch_op:
            for column in missing_columns:
                batch_op.add_column(column)
    _create_index_if_missing(
        RETRY_INDEX_NAME,
        "generation_items",
        ["status", "retry_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "generation_items" in _table_names():
        reconnecting = bind.execute(
            sa.text("SELECT 1 FROM generation_items WHERE status = 'reconnecting' LIMIT 1")
        ).first()
        if reconnecting is not None:
            raise RuntimeError("降级前必须先完成或取消所有正在重连的生成任务")

    if "generation_jobs" in _table_names():
        _drop_index_if_exists(INDEX_NAME, "generation_jobs")
        _workspace_active_index(reconnecting=False)

    _drop_index_if_exists(RETRY_INDEX_NAME, "generation_items")
    existing_columns = _existing_columns("generation_items")
    columns_to_drop = [
        name for name in ("retry_at", "retry_limit", "retry_count") if name in existing_columns
    ]
    if columns_to_drop:
        with op.batch_alter_table("generation_items", schema=None) as batch_op:
            for column_name in columns_to_drop:
                batch_op.drop_column(column_name)
