"""Discard chat model configuration that used separate model IDs."""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6d3e8a4c2b19"
down_revision: str | Sequence[str] | None = "1f3a9c7e2b64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _settings_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


def upgrade() -> None:
    bind = op.get_bind()
    states = sa.table("system_state", sa.column("key", sa.String()))
    bind.execute(states.delete().where(states.c.key == "runtime_config.chat_models.v1"))

    workspaces = sa.table(
        "workspaces", sa.column("id", sa.String()), sa.column("settings", sa.JSON())
    )
    rows = bind.execute(sa.select(workspaces.c.id, workspaces.c.settings))
    for row in rows.mappings():
        settings = _settings_dict(row["settings"])
        if settings is None or "chat_model_id" not in settings:
            continue
        settings.pop("chat_model_id")
        bind.execute(
            workspaces.update().where(workspaces.c.id == row["id"]).values(settings=settings)
        )


def downgrade() -> None:
    # Deleted configuration and selections cannot be reconstructed.
    pass
