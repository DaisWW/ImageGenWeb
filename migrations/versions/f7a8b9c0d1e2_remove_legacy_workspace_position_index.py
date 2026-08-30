"""Remove an index left by an intermediate workspace schema."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_workspaces_user_position"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "workspaces" not in inspector.get_table_names():
        return
    index_names = {index["name"] for index in inspector.get_indexes("workspaces")}
    if INDEX_NAME in index_names:
        op.drop_index(INDEX_NAME, table_name="workspaces")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "workspaces" not in inspector.get_table_names():
        return
    index_names = {index["name"] for index in inspector.get_indexes("workspaces")}
    if INDEX_NAME not in index_names:
        op.create_index(INDEX_NAME, "workspaces", ["user_id", "position"], unique=False)
