"""Use saved channel list order instead of a priority field."""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e4c2a9b1d63"
down_revision: str | Sequence[str] | None = "6d3e8a4c2b19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHANNEL_CONFIG_KEY = "runtime_config.channels.v1"


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


def upgrade() -> None:
    bind = op.get_bind()
    states = sa.table(
        "system_state",
        sa.column("key", sa.String()),
        sa.column("value", sa.Text()),
    )
    row = bind.execute(
        sa.select(states.c.value).where(states.c.key == _CHANNEL_CONFIG_KEY)
    ).mappings().first()
    if row is None:
        return

    payload = _mapping(row["value"])
    document = payload.get("document") if payload else None
    channels = document.get("channels") if isinstance(document, dict) else None
    if not isinstance(payload, dict) or not isinstance(document, dict) or not isinstance(channels, list):
        return

    changed = False
    for channel in channels:
        if isinstance(channel, dict) and "priority" in channel:
            channel.pop("priority")
            changed = True
    if not changed:
        return

    bind.execute(
        states.update()
        .where(states.c.key == _CHANNEL_CONFIG_KEY)
        .values(
            value=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    )


def downgrade() -> None:
    # Removed priority values cannot be reconstructed after list reordering.
    pass
