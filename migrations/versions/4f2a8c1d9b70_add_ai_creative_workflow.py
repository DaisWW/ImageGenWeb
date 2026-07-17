"""新增 AI 创作工作流与图片验收

修订 ID：4f2a8c1d9b70
上一修订：d2f6a8b4c901
创建时间：2026-07-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4f2a8c1d9b70"
down_revision: Union[str, Sequence[str], None] = "d2f6a8b4c901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def upgrade() -> None:
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workflow", JSON_TYPE, nullable=False, server_default=sa.text("'{}'"))
        )
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("review", JSON_TYPE, nullable=False, server_default=sa.text("'{}'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("review")
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.drop_column("workflow")
