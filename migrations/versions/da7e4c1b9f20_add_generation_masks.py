"""Persist local-edit masks with generation jobs."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "da7e4c1b9f20"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UNIQUE_NAME = "uq_generation_jobs_mask_storage_path"
CHECK_NAME = "ck_generation_jobs_mask_complete"
INDEX_NAME = "ix_generation_jobs_mask_target_asset_id"
FOREIGN_KEY_NAME = "fk_generation_jobs_mask_target_asset_id_assets"


def upgrade() -> None:
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mask_target_asset_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("mask_storage_path", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("mask_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("mask_byte_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("mask_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("mask_height", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            FOREIGN_KEY_NAME,
            "assets",
            ["mask_target_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(UNIQUE_NAME, ["mask_storage_path"])
        batch_op.create_check_constraint(
            CHECK_NAME,
            "(mask_storage_path IS NULL AND mask_target_asset_id IS NULL "
            "AND mask_sha256 IS NULL AND mask_byte_count IS NULL "
            "AND mask_width IS NULL AND mask_height IS NULL) OR "
            "(mask_storage_path IS NOT NULL AND mask_target_asset_id IS NOT NULL "
            "AND mask_sha256 IS NOT NULL AND mask_byte_count IS NOT NULL "
            "AND mask_byte_count > 0 AND mask_width IS NOT NULL "
            "AND mask_width > 0 AND mask_height IS NOT NULL AND mask_height > 0)",
        )
        batch_op.create_index(INDEX_NAME, ["mask_target_asset_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.drop_index(INDEX_NAME)
        batch_op.drop_constraint(CHECK_NAME, type_="check")
        batch_op.drop_constraint(UNIQUE_NAME, type_="unique")
        batch_op.drop_constraint(FOREIGN_KEY_NAME, type_="foreignkey")
        batch_op.drop_column("mask_height")
        batch_op.drop_column("mask_width")
        batch_op.drop_column("mask_byte_count")
        batch_op.drop_column("mask_sha256")
        batch_op.drop_column("mask_storage_path")
        batch_op.drop_column("mask_target_asset_id")
