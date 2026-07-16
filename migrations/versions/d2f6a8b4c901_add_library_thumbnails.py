"""Add image library thumbnails and remove a redundant index.

Revision ID: d2f6a8b4c901
Revises: a7c3e9f1b2d4
Create Date: 2026-07-16 00:00:01.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2f6a8b4c901"
down_revision: Union[str, Sequence[str], None] = "a7c3e9f1b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "library_images",
        sa.Column("thumbnail_path", sa.String(length=500), nullable=True),
    )
    op.drop_index(op.f("ix_library_images_user_id"), table_name="library_images")


def downgrade() -> None:
    op.create_index(
        op.f("ix_library_images_user_id"),
        "library_images",
        ["user_id"],
        unique=False,
    )
    op.drop_column("library_images", "thumbnail_path")
