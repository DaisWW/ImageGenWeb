"""Remove the frame-animation workflow.

Revision ID: 7c4e2a9b1d63
Revises: 6a1b2c3d4e5f
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c4e2a9b1d63"
down_revision: str | Sequence[str] | None = "6a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANIMATION_SETTING_KEYS = {
    "animation_frame_count",
    "animation_fps",
    "animation_loop",
    "animation_format",
}
_RUNTIME_SETTING_KEYS = {"max_animation_frames", "max_animation_fps"}
_RUNTIME_CONFIG_KEY = "runtime_config.system.v1"
_CHAT_CONFIG_KEY = "runtime_config.chat_models.v1"


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


def _clean_saved_configs(bind: sa.Connection) -> None:
    states = sa.table(
        "system_state",
        sa.column("key", sa.String()),
        sa.column("value", sa.Text()),
    )
    rows = bind.execute(
        sa.select(states.c.key, states.c.value).where(
            states.c.key.in_((_RUNTIME_CONFIG_KEY, _CHAT_CONFIG_KEY))
        )
    )
    for row in rows.mappings():
        payload = _settings_dict(row["value"])
        if payload is None:
            continue
        changed = False
        if row["key"] == _RUNTIME_CONFIG_KEY:
            for key in _RUNTIME_SETTING_KEYS:
                if key in payload:
                    payload.pop(key)
                    changed = True
        else:
            document = payload.get("document")
            prompts = document.get("workspace_prompts") if isinstance(document, dict) else None
            if isinstance(prompts, dict) and "animation" in prompts:
                prompts.pop("animation")
                changed = True
        if changed:
            bind.execute(
                states.update()
                .where(states.c.key == row["key"])
                .values(
                    value=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            )


def upgrade() -> None:
    bind = op.get_bind()
    _clean_saved_configs(bind)
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("settings", sa.JSON()),
    )
    rows = bind.execute(sa.select(workspaces.c.id, workspaces.c.kind, workspaces.c.settings))
    for row in rows.mappings():
        values: dict[str, object] = {}
        if row["kind"] == "animation":
            values["kind"] = "image"
        settings = _settings_dict(row["settings"])
        if settings is not None and any(key in settings for key in _ANIMATION_SETTING_KEYS):
            for key in _ANIMATION_SETTING_KEYS:
                settings.pop(key, None)
            values["settings"] = settings
        if values:
            bind.execute(workspaces.update().where(workspaces.c.id == row["id"]).values(**values))

    jobs = sa.table(
        "generation_jobs",
        sa.column("kind", sa.String()),
    )
    bind.execute(
        jobs.update().where(jobs.c.kind.in_(("animation", "animation_master"))).values(kind="image")
    )
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.drop_column("animation_format")
        batch_op.drop_column("animation_loop")
        batch_op.drop_column("animation_fps")


def downgrade() -> None:
    with op.batch_alter_table("generation_jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("animation_fps", sa.Integer(), server_default="8", nullable=False)
        )
        batch_op.add_column(
            sa.Column("animation_loop", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "animation_format",
                sa.String(length=20),
                server_default="webp",
                nullable=False,
            )
        )
