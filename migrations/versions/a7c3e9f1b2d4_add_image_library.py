"""Add account image library.

Revision ID: a7c3e9f1b2d4
Revises: f3a8c91d7e20
Create Date: 2026-07-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9f1b2d4"
down_revision: Union[str, Sequence[str], None] = "f3a8c91d7e20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "library_images",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=50), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_path"),
        sa.UniqueConstraint("user_id", "sha256", name="uq_library_images_user_sha256"),
    )
    op.create_index(
        "ix_library_images_user_created",
        "library_images",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_library_images_user_id"),
        "library_images",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_library_images_user_id"), table_name="library_images")
    op.drop_index("ix_library_images_user_created", table_name="library_images")
    op.drop_table("library_images")
