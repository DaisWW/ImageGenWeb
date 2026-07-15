from __future__ import annotations

from datetime import datetime, timedelta

from .models import utcnow


def worker_heartbeat_grace_seconds(heartbeat_seconds: int) -> int:
    return max(30, heartbeat_seconds * 3)


def worker_heartbeat_is_fresh(
    heartbeat_at: datetime | None,
    heartbeat_seconds: int,
    *,
    now: datetime | None = None,
) -> bool:
    if heartbeat_at is None:
        return False
    current = now or utcnow()
    if heartbeat_at.tzinfo is None:
        current = current.replace(tzinfo=None)
    grace_seconds = worker_heartbeat_grace_seconds(heartbeat_seconds)
    return heartbeat_at >= current - timedelta(seconds=grace_seconds)
