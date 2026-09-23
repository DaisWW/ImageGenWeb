"""Remove image review and series-anchor workflows.

Revision ID: 1f3a9c7e2b64
Revises: da7e4c1b9f20

The JSON cleanup is intentionally irreversible; downgrade only restores an
empty compatibility column for the removed review data.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1f3a9c7e2b64"
down_revision: str | Sequence[str] | None = "da7e4c1b9f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def _mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


def _clean_workspace_settings(bind: sa.Connection) -> None:
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.String()),
        sa.column("settings", sa.JSON()),
    )
    rows = bind.execute(sa.select(workspaces.c.id, workspaces.c.settings))
    for row in rows.mappings():
        settings = _mapping(row["settings"])
        if settings is None:
            continue
        changed = "series_anchor" in settings
        settings.pop("series_anchor", None)
        if str(settings.get("generation_strategy", "")).strip().lower() == "series":
            settings["generation_strategy"] = "sample"
            changed = True
        if changed:
            bind.execute(
                workspaces.update().where(workspaces.c.id == row["id"]).values(settings=settings)
            )


def _clean_json_records(bind: sa.Connection, table_name: str, column_name: str) -> None:
    records = sa.table(
        table_name,
        sa.column("id", sa.String()),
        sa.column(column_name, sa.JSON()),
    )
    column = records.c[column_name]
    rows = bind.execute(sa.select(records.c.id, column))
    for row in rows.mappings():
        value = _mapping(row[column_name])
        if value is None:
            continue
        changed = False
        for key in ("series_anchor", "series_contract"):
            if key in value:
                value.pop(key)
                changed = True
        if str(value.get("generation_strategy", "")).strip().lower() == "series":
            value["generation_strategy"] = "sample"
            changed = True
        if changed:
            bind.execute(
                records.update().where(records.c.id == row["id"]).values({column_name: value})
            )


def upgrade() -> None:
    bind = op.get_bind()
    _clean_workspace_settings(bind)
    _clean_json_records(bind, "conversation_messages", "payload")
    _clean_json_records(bind, "generation_jobs", "workflow")
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.drop_column("review")


def downgrade() -> None:
    with op.batch_alter_table("generation_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("review", JSON_TYPE, nullable=False, server_default=sa.text("'{}'"))
        )
