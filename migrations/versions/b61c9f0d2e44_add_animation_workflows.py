"""新增动画工作流

修订 ID：b61c9f0d2e44
上一修订：8d7f4b2c1a90
创建时间：2026-07-15 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b61c9f0d2e44"
down_revision: Union[str, Sequence[str], None] = "8d7f4b2c1a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=20), server_default="image", nullable=False)
        )
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=20), server_default="image", nullable=False)
        )
        batch_op.add_column(
            sa.Column("animation_fps", sa.Integer(), server_default="6", nullable=False)
        )
        batch_op.add_column(
            sa.Column("animation_loop", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "animation_format",
                sa.String(length=20),
                server_default="webp",
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.drop_column("animation_format")
        batch_op.drop_column("animation_loop")
        batch_op.drop_column("animation_fps")
        batch_op.drop_column("kind")
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("kind")
