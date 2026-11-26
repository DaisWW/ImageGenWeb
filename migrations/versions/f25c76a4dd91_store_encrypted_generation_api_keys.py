"""Store a retrievable copy of personal generation API keys."""

import sqlalchemy as sa
from alembic import op

revision = "f25c76a4dd91"
down_revision = "d17c62af909e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_api_keys", sa.Column("token_encrypted", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("generation_api_keys") as batch:
        batch.drop_column("token_encrypted")
