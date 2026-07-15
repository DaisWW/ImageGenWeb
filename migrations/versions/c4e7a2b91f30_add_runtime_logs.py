"""新增结构化运行日志

修订 ID：c4e7a2b91f30
上一修订：b61c9f0d2e44
创建时间：2026-07-15 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e7a2b91f30"
down_revision: Union[str, Sequence[str], None] = "b61c9f0d2e44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_logs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=12), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_label", sa.String(length=120), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_label", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("item_id", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("provider_label", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=150), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("upstream_request_id", sa.String(length=255), nullable=False),
        sa.Column("elapsed_seconds", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runtime_logs", schema=None) as batch_op:
        batch_op.create_index("ix_runtime_logs_created", ["created_at"], unique=False)
        batch_op.create_index(
            "ix_runtime_logs_category_status_created",
            ["category", "status", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_runtime_logs_user_created", ["user_id", "created_at"], unique=False
        )
        batch_op.create_index("ix_runtime_logs_error_code", ["error_code"], unique=False)
        batch_op.create_index("ix_runtime_logs_level", ["level"], unique=False)
        batch_op.create_index("ix_runtime_logs_category", ["category"], unique=False)
        batch_op.create_index("ix_runtime_logs_event", ["event"], unique=False)
        batch_op.create_index("ix_runtime_logs_status", ["status"], unique=False)


def downgrade() -> None:
    op.drop_table("runtime_logs")
