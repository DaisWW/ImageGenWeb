"""Scope generation API keys to their account."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "d17c62af909e"
down_revision = "53e1c8d409af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the original ID for downgrade without attaching key validity to the workspace.
    op.add_column("generation_api_keys", sa.Column("legacy_workspace_id", sa.String(32)))
    op.execute(sa.text("UPDATE generation_api_keys SET legacy_workspace_id = workspace_id"))
    with op.batch_alter_table("generation_api_keys") as batch:
        batch.drop_index("ix_generation_api_keys_workspace_id")
        batch.drop_column("workspace_id")


def downgrade() -> None:
    op.add_column("generation_api_keys", sa.Column("workspace_id", sa.String(32)))
    keys = sa.table(
        "generation_api_keys",
        sa.column("user_id", sa.Integer),
        sa.column("workspace_id", sa.String),
        sa.column("legacy_workspace_id", sa.String),
    )
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.String),
        sa.column("user_id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("name", sa.String),
        sa.column("position", sa.Integer),
        sa.column("settings", sa.JSON),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    bind = op.get_bind()
    # Restore the old binding when possible, then use an archive or a regular workspace.
    original_exists = (
        sa.select(workspaces.c.id)
        .where(
            workspaces.c.id == keys.c.legacy_workspace_id,
            workspaces.c.user_id == keys.c.user_id,
            workspaces.c.kind == "image",
        )
        .exists()
    )
    bind.execute(
        keys.update().where(original_exists).values(workspace_id=keys.c.legacy_workspace_id)
    )
    for kind in ("generation_api", "image"):
        destination = (
            sa.select(workspaces.c.id)
            .where(workspaces.c.user_id == keys.c.user_id, workspaces.c.kind == kind)
            .order_by(workspaces.c.updated_at.desc(), workspaces.c.id)
            .limit(1)
            .scalar_subquery()
        )
        bind.execute(
            keys.update().where(keys.c.workspace_id.is_(None)).values(workspace_id=destination)
        )
    for user_id in bind.scalars(
        sa.select(keys.c.user_id).where(keys.c.workspace_id.is_(None))
    ).all():
        workspace_id = uuid4().hex
        bind.execute(
            workspaces.insert().values(
                id=workspace_id,
                user_id=user_id,
                name=f"生图 API {workspace_id}",
                kind="image",
                position=0,
                settings={},
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )
        bind.execute(
            keys.update().where(keys.c.user_id == user_id).values(workspace_id=workspace_id)
        )
    bind.execute(
        workspaces.update().where(workspaces.c.kind == "generation_api").values(kind="image")
    )
    with op.batch_alter_table("generation_api_keys") as batch:
        batch.drop_column("legacy_workspace_id")
        batch.alter_column("workspace_id", existing_type=sa.String(32), nullable=False)
        batch.create_foreign_key(
            "generation_api_keys_workspace_id_fkey",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_generation_api_keys_workspace_id", ["workspace_id"])
