"""Add administrator image test suites and runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "53e1c8d409af"
down_revision = "2a6d8c4f1b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    questions_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "image_test_suites",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("questions", questions_type, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "image_test_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("suite_name", sa.String(80), nullable=False),
        sa.Column("questions", questions_type, nullable=False),
        sa.Column("total_images", sa.Integer(), nullable=False),
        sa.Column("estimated_rmb", sa.Numeric(16, 4), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("retry_of", sa.String(32)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_image_test_runs_created_at", "image_test_runs", ["created_at"])
    with op.batch_alter_table("generation_jobs") as batch:
        batch.add_column(sa.Column("image_test_run_id", sa.String(32)))
        batch.add_column(sa.Column("image_test_question_index", sa.Integer()))
        batch.add_column(sa.Column("image_test_verdict", sa.String(10)))
        batch.create_foreign_key(
            "fk_generation_jobs_image_test_run",
            "image_test_runs",
            ["image_test_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_generation_jobs_image_test_run_id", ["image_test_run_id"])


def downgrade() -> None:
    # Older workers have no measurement billing bypass. Preserve finished files
    # and stop unfinished or retryable tests before the old app can run them.
    test_jobs = "SELECT id FROM generation_jobs WHERE image_test_run_id IS NOT NULL"
    op.execute(
        sa.text(f"""
        UPDATE generation_attempts
        SET status = 'canceled', completed_at = CURRENT_TIMESTAMP,
            claimed_by = NULL, heartbeat_at = NULL, circuit_probe = false
        WHERE status IN ('running', 'retrying')
          AND item_id IN (SELECT id FROM generation_items WHERE job_id IN ({test_jobs}))
    """)
    )
    op.execute(
        sa.text(f"""
        UPDATE generation_items
        SET status = 'canceled', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
            cancel_requested_at = CURRENT_TIMESTAMP, retry_at = NULL,
            claimed_by = NULL, heartbeat_at = NULL, circuit_probe = false
        WHERE job_id IN ({test_jobs})
          AND status IN ('queued', 'running', 'reconnecting', 'canceling', 'failed', 'interrupted')
    """)
    )
    op.execute(
        sa.text("""
        UPDATE generation_jobs
        SET status = 'canceled', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
            cancel_requested_at = CURRENT_TIMESTAMP
        WHERE image_test_run_id IS NOT NULL
          AND status IN ('queued', 'running', 'reconnecting', 'canceling', 'failed', 'partial')
    """)
    )
    with op.batch_alter_table("generation_jobs") as batch:
        batch.drop_index("ix_generation_jobs_image_test_run_id")
        batch.drop_constraint("fk_generation_jobs_image_test_run", type_="foreignkey")
        batch.drop_column("image_test_verdict")
        batch.drop_column("image_test_question_index")
        batch.drop_column("image_test_run_id")
    op.drop_table("image_test_runs")
    op.drop_table("image_test_suites")
