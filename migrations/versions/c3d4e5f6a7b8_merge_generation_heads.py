"""Merge the generation and background-removal migration branches."""

from typing import Sequence, Union

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = (
    "a8c6e2f1d4b0",
    "b8e4c2d7f1a6",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
