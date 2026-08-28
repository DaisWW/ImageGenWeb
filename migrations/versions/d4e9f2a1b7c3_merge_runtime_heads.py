"""Merge the generation and background-removal migration branches.

Revision ID: d4e9f2a1b7c3
Revises: a8c6e2f1d4b0, c2d7e9f1a4b6
Create Date: 2026-08-28 00:00:00.000000
"""

from typing import Sequence, Union

revision: str = "d4e9f2a1b7c3"
down_revision: Union[str, Sequence[str], None] = ("a8c6e2f1d4b0", "c2d7e9f1a4b6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
