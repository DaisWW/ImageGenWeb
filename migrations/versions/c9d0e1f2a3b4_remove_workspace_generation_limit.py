"""Remove account and workspace generation limits."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_generation_jobs_workspace_active"
USER_CONCURRENCY_COLUMN = "generation_concurrency"
USER_CONCURRENCY_CONSTRAINT = "ck_users_concurrency"
QUEUE_STATE_TABLE = "generation_queue_state"
ACTIVE_STATUSES = "'queued', 'running', 'canceling', 'reconnecting'"
USERNAME_INDEX = "uq_users_username_lower"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {str(index["name"]) for index in inspector.get_indexes(table_name)}


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _check_constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {
        str(constraint["name"])
        for constraint in inspector.get_check_constraints(table_name)
        if constraint.get("name")
    }


def _restore_sqlite_username_index() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and "users" in _table_names():
        bind.execute(
            sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {USERNAME_INDEX} ON users (lower(username))"
            )
        )


def _drop_user_concurrency_column(constraints: set[str]) -> None:
    bind = op.get_bind()

    def alter_users() -> None:
        with op.batch_alter_table("users", schema=None) as batch_op:
            if USER_CONCURRENCY_CONSTRAINT in constraints:
                batch_op.drop_constraint(USER_CONCURRENCY_CONSTRAINT, type_="check")
            batch_op.drop_column(USER_CONCURRENCY_COLUMN)

    if bind.dialect.name != "sqlite":
        alter_users()
        return

    # SQLite rebuilds the table for a column drop.  ``migrations/env.py``
    # disables foreign-key checks for the migration transaction so this
    # referenced table can be rebuilt without committing the Alembic version.
    alter_users()


def upgrade() -> None:
    if INDEX_NAME in _index_names("generation_jobs"):
        op.drop_index(INDEX_NAME, table_name="generation_jobs")

    if USER_CONCURRENCY_COLUMN in _column_names("users"):
        constraints = _check_constraint_names("users")
        _drop_user_concurrency_column(constraints)
        _restore_sqlite_username_index()

    if QUEUE_STATE_TABLE in _table_names():
        op.drop_table(QUEUE_STATE_TABLE)

    # Older databases created conversation state lazily. Seed missing rows so
    # existing workspaces share the invariant used by newly created ones.
    op.execute(
        sa.text(
            "INSERT INTO conversation_state "
            "(workspace_id, summary, summary_through_message_id, "
            "estimated_context_tokens, updated_at) "
            "SELECT workspaces.id, '', '', 0, CURRENT_TIMESTAMP "
            "FROM workspaces "
            "LEFT JOIN conversation_state "
            "ON conversation_state.workspace_id = workspaces.id "
            "WHERE conversation_state.workspace_id IS NULL"
        )
    )


def downgrade() -> None:
    if "generation_jobs" in _table_names():
        duplicate = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT workspace_id FROM generation_jobs "
                    f"WHERE status IN ({ACTIVE_STATUSES}) "
                    "GROUP BY workspace_id HAVING COUNT(*) > 1 LIMIT 1"
                )
            )
            .first()
        )
        if duplicate is not None:
            raise RuntimeError("降级前必须先完成或取消同一工作站中的重复活动生成任务")

    if QUEUE_STATE_TABLE not in _table_names():
        op.create_table(
            QUEUE_STATE_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.execute(
            sa.text(
                "INSERT INTO generation_queue_state (id, updated_at) VALUES (1, CURRENT_TIMESTAMP)"
            )
        )

    if USER_CONCURRENCY_COLUMN not in _column_names("users"):
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    USER_CONCURRENCY_COLUMN,
                    sa.Integer(),
                    server_default="2",
                    nullable=False,
                )
            )
            batch_op.create_check_constraint(
                USER_CONCURRENCY_CONSTRAINT,
                "generation_concurrency BETWEEN 1 AND 16",
            )
        _restore_sqlite_username_index()

    if INDEX_NAME not in _index_names("generation_jobs"):
        predicate = sa.text(f"status IN ({ACTIVE_STATUSES})")
        op.create_index(
            INDEX_NAME,
            "generation_jobs",
            ["workspace_id"],
            unique=True,
            sqlite_where=predicate,
            postgresql_where=predicate,
        )
