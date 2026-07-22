"""Persist the effective prompt for each generated item."""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "9d2f5a7c1e84"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prompt", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE generation_items "
            "SET prompt = (SELECT generation_jobs.prompt FROM generation_jobs "
            "WHERE generation_jobs.id = generation_items.job_id)"
        )
    )
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.alter_column("prompt", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("prompt")
