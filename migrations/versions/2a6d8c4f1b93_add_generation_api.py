"""Add personal generation API keys and idempotent submissions."""

import sqlalchemy as sa
from alembic import op

revision = "2a6d8c4f1b93"
down_revision = "7e4c2a9b1d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_api_keys",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("token_digest", name="uq_generation_api_keys_digest"),
    )
    op.create_index("ix_generation_api_keys_workspace_id", "generation_api_keys", ["workspace_id"])
    op.add_column("generation_jobs", sa.Column("api_request_id", sa.String(length=32)))
    op.add_column("generation_jobs", sa.Column("api_request_digest", sa.String(length=64)))
    op.create_index(
        "uq_generation_jobs_user_api_request",
        "generation_jobs",
        ["user_id", "api_request_id"],
        unique=True,
        postgresql_where=sa.text("api_request_id IS NOT NULL"),
        sqlite_where=sa.text("api_request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_generation_jobs_user_api_request", table_name="generation_jobs")
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_column("api_request_digest")
        batch_op.drop_column("api_request_id")
    op.drop_table("generation_api_keys")
