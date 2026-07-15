"""新增队列与 Worker 状态

修订 ID：f3a8c91d7e20
上一修订：e91f7c4a2d6b
创建时间：2026-07-15 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a8c91d7e20"
down_revision: Union[str, Sequence[str], None] = "e91f7c4a2d6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generation_queue_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text("INSERT INTO generation_queue_state (id, updated_at) VALUES (1, CURRENT_TIMESTAMP)")
    )
    op.create_table(
        "worker_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text("INSERT INTO worker_state (id, worker_id, heartbeat_at) VALUES (1, NULL, NULL)")
    )
    op.create_index(
        "uq_users_username_lower",
        "users",
        [sa.text("lower(username)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_table("worker_state")
    op.drop_table("generation_queue_state")
