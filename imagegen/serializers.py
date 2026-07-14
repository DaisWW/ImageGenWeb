from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from flask import url_for

from .config.channels import ChannelRegistry
from .models import (
    Asset,
    ConversationMessage,
    GenerationItem,
    GenerationJob,
    User,
    WalletLedger,
    Workspace,
    utcnow,
)

TERMINAL_STATUSES = {"succeeded", "failed", "canceled", "interrupted"}


def user_dict(user: User, *, include_private: bool = True) -> dict[str, Any]:
    result = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "generation_concurrency": user.generation_concurrency,
    }
    if include_private:
        result.update(
            balance_rmb=_amount(user.balance_rmb),
            reserved_rmb=_amount(user.reserved_rmb),
            available_balance_rmb=_amount(user.available_balance_rmb),
            created_at=_iso(user.created_at),
            last_login_at=_iso(user.last_login_at),
        )
    return result


def workspace_dict(workspace: Workspace, assets: list[Asset] | None = None) -> dict[str, Any]:
    if assets is None:
        assets = sorted(
            (asset for asset in workspace.assets if asset.deleted_at is None),
            key=lambda item: (item.position, item.created_at),
        )
    return {
        "id": workspace.id,
        "name": workspace.name,
        "settings": workspace.settings,
        "created_at": _iso(workspace.created_at),
        "updated_at": _iso(workspace.updated_at),
        "assets": [asset_dict(asset) for asset in assets],
    }


def asset_dict(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "name": asset.original_name,
        "mime_type": asset.mime_type,
        "bytes": asset.byte_count,
        "width": asset.width,
        "height": asset.height,
        "position": asset.position,
        "url": url_for("web.asset_file", asset_id=asset.id),
    }


def conversation_message_dict(message: ConversationMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "kind": message.kind,
        "content": message.content,
        "payload": message.payload or {},
        "provider_id": message.provider_id,
        "provider_label": message.provider_label,
        "model": message.model,
        "input_tokens": message.input_tokens,
        "output_tokens": message.output_tokens,
        "elapsed_seconds": (
            float(message.elapsed_seconds) if message.elapsed_seconds is not None else None
        ),
        "created_at": _iso(message.created_at),
        "attachments": [asset_dict(attachment.asset) for attachment in message.attachments],
    }


def job_dict(
    job: GenerationJob,
    channels: ChannelRegistry,
    *,
    queue_position: int | None = None,
    queue_total: int = 0,
    admin: bool = False,
) -> dict[str, Any]:
    now = utcnow()
    item_results = [item_dict(item, now=now, admin=admin) for item in job.items]
    item_progress = [item["progress_percent"] for item in item_results]
    progress = round(sum(item_progress) / len(item_progress)) if item_progress else 0
    if job.status == "queued":
        progress = 0
    elif job.status in {"succeeded", "failed", "partial", "canceled"}:
        progress = 100
    estimated_end = _job_estimated_end(job, channels, now)
    succeeded = sum(item.status == "succeeded" for item in job.items)
    failed = sum(item.status in {"failed", "interrupted"} for item in job.items)
    canceled = sum(item.status == "canceled" for item in job.items)
    result = {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "channel_id": job.channel_id,
        "channel": job.channel_label,
        "mode": job.mode,
        "prompt": job.prompt,
        "model": job.model,
        "size": job.size,
        "quality": job.quality,
        "output_format": job.output_format,
        "compression": job.compression,
        "transparent_background": job.transparent_background,
        "requested_count": job.requested_count,
        "price_per_image_rmb": _amount(job.price_per_image_rmb),
        "charged_rmb": _amount(job.charged_rmb),
        "reserved_rmb": _amount(job.reserved_rmb),
        "status": job.status,
        "progress_percent": progress,
        "queue_position": queue_position if job.status == "queued" else None,
        "queue_total": queue_total if job.status == "queued" else 0,
        "estimated_end_at": _iso(estimated_end),
        "is_over_estimate": bool(
            estimated_end and now > estimated_end and job.status in {"running", "canceling"}
        ),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "succeeded_count": succeeded,
        "failed_count": failed,
        "canceled_count": canceled,
        "can_cancel": job.status in {"queued", "running", "canceling"},
        "references": [asset_dict(reference.asset) for reference in job.references],
        "items": item_results,
    }
    if admin:
        result["user"] = user_dict(job.user, include_private=False)
        result["config_version"] = job.channel_config_version[:12]
    return result


def item_dict(item: GenerationItem, *, now: datetime, admin: bool = False) -> dict[str, Any]:
    progress = _item_progress(item, now)
    expected_end = None
    if item.started_at and item.estimated_seconds:
        expected_end = _aware(item.started_at) + timedelta(seconds=float(item.estimated_seconds))
    result = {
        "id": item.id,
        "position": item.position,
        "status": item.status,
        "progress_percent": progress,
        "started_at": _iso(item.started_at),
        "completed_at": _iso(item.completed_at),
        "estimated_seconds": float(item.estimated_seconds) if item.estimated_seconds else None,
        "estimated_end_at": _iso(expected_end),
        "elapsed_seconds": float(item.elapsed_seconds)
        if item.elapsed_seconds is not None
        else None,
        "charged_rmb": _amount(item.charged_rmb),
        "error": item.error_message,
        "width": item.output_width,
        "height": item.output_height,
        "bytes": item.output_byte_count,
        "image_url": url_for("web.output_file", item_id=item.id) if item.output_path else None,
        "thumbnail_url": url_for("web.output_thumbnail", item_id=item.id)
        if item.thumbnail_path
        else None,
        "download_url": url_for("web.output_file", item_id=item.id, download=1)
        if item.output_path
        else None,
    }
    if admin:
        result.update(
            upstream_status=item.upstream_status,
            upstream_request_id=item.upstream_request_id,
            error_code=item.error_code,
        )
    return result


def ledger_dict(entry: WalletLedger) -> dict[str, Any]:
    return {
        "id": entry.id,
        "type": entry.entry_type,
        "amount_rmb": _signed_amount(entry.amount_rmb),
        "balance_after_rmb": _amount(entry.balance_after_rmb),
        "note": entry.note,
        "created_at": _iso(entry.created_at),
    }


def _item_progress(item: GenerationItem, now: datetime) -> int:
    if item.status == "queued":
        return 0
    if item.status in TERMINAL_STATUSES:
        return 100
    if not item.started_at or not item.estimated_seconds:
        return 1
    elapsed = max(0.0, (now - _aware(item.started_at)).total_seconds())
    estimate = max(float(item.estimated_seconds), 1.0)
    return min(95, max(1, round(elapsed / estimate * 90)))


def _job_estimated_end(
    job: GenerationJob, channels: ChannelRegistry, now: datetime
) -> datetime | None:
    if job.status not in {"running", "canceling"} or not job.started_at:
        return None
    estimates = [float(item.estimated_seconds) for item in job.items if item.estimated_seconds]
    typical = statistics_median(estimates) if estimates else 180.0
    active_ends = [
        _aware(item.started_at) + timedelta(seconds=float(item.estimated_seconds or typical))
        for item in job.items
        if item.status in {"running", "canceling"} and item.started_at
    ]
    base = max(active_ends, default=now)
    queued = sum(item.status == "queued" for item in job.items)
    try:
        channel = channels.get(job.channel_id, require_available=False)
        slots = max(1, min(channel.limits.max_concurrency, job.user.generation_concurrency))
    except ValueError:
        slots = 1
    waves = math.ceil(queued / slots)
    return base + timedelta(seconds=waves * typical)


def statistics_median(values: list[float]) -> float:
    values = sorted(values)
    count = len(values)
    if not count:
        return 0.0
    middle = count // 2
    return values[middle] if count % 2 else (values[middle - 1] + values[middle]) / 2


def _amount(value: Decimal | int | str) -> str:
    return format(Decimal(value).quantize(Decimal("0.0001")), ".4f")


def display_amount(value: Decimal | int | str) -> str:
    whole, fraction = _amount(value).split(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def _signed_amount(value: Decimal | int | str) -> str:
    amount = Decimal(value).quantize(Decimal("0.0001"))
    return f"{amount:+.4f}"


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value else None
