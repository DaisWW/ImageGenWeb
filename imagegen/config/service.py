from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import GenerationItem
from ..validation import as_bool
from .channels import ChannelRegistry, ChannelSnapshot
from .chat_models import ChatModelRegistry
from .repository import RuntimeConfigRepository

ACTIVE_GENERATION_STATUSES = {"queued", "running", "canceling"}


class RuntimeConfigService:
    """Validates and publishes administrator-managed runtime configuration."""

    def __init__(
        self,
        repository: RuntimeConfigRepository,
        channels: ChannelRegistry,
        chat_models: ChatModelRegistry,
    ):
        self.repository = repository
        self.channels = channels
        self.chat_models = chat_models

    def channel_config(self) -> dict[str, Any]:
        config = self.channels.editable_config()
        config["revision"] = self.repository.channel_revision()
        config["managed"] = bool(config["revision"])
        return config

    def chat_config(self) -> dict[str, Any]:
        config = self.chat_models.editable_config()
        config["revision"] = self.repository.chat_revision()
        config["managed"] = bool(config["revision"])
        return config

    def save_channels(self, payload: Any, actor_user_id: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ServiceError("渠道配置必须是对象")
        document = self._channel_document(payload)
        snapshot = self.channels.validate(document)
        self._guard_active_channels(snapshot)
        self.repository.save_channels(
            document,
            expected_revision=str(payload.get("revision", "")),
            actor_user_id=actor_user_id,
        )
        if not self.channels.reload(force=True):
            raise ServiceError("渠道配置已保存但未能加载", status_code=500)
        return self.channel_config()

    def save_chat_models(self, payload: Any, actor_user_id: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ServiceError("对话模型配置必须是对象")
        document = self._chat_document(payload)
        self.chat_models.validate(document)
        self.repository.save_chat_models(
            document,
            expected_revision=str(payload.get("revision", "")),
            actor_user_id=actor_user_id,
        )
        if not self.chat_models.reload(force=True):
            raise ServiceError("对话模型配置已保存但未能加载", status_code=500)
        return self.chat_config()

    def _channel_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_channels = payload.get("channels")
        if not isinstance(raw_channels, list):
            raise ServiceError("渠道列表格式无效")
        existing = {
            channel.identifier: channel for channel in self.channels.list(include_disabled=True)
        }
        channels: list[dict[str, Any]] = []
        for raw in raw_channels:
            if not isinstance(raw, dict):
                raise ServiceError("渠道条目格式无效")
            identifier = str(raw.get("id", "")).strip()
            old = existing.get(identifier)
            channels.append(
                {
                    "id": identifier,
                    "label": str(raw.get("label", "")).strip(),
                    "enabled": as_bool(raw.get("enabled", True)),
                    "adapter": "openai_images",
                    "base_url": str(raw.get("base_url", "")).strip(),
                    "api_key": _resolved_key(raw, old.api_key if old else ""),
                    "models": _models(raw.get("models")),
                    "price_rmb": raw.get("price_rmb", "0"),
                    "capabilities": {
                        "modes": _strings(raw.get("capabilities"), "modes"),
                        "max_reference_images": _nested(
                            raw, "capabilities", "max_reference_images"
                        ),
                        "max_reference_image_mb": _nested(
                            raw, "capabilities", "max_reference_image_mb"
                        ),
                        "max_reference_total_mb": _nested(
                            raw, "capabilities", "max_reference_total_mb"
                        ),
                        "reference_field": str(
                            _nested(raw, "capabilities", "reference_field") or "image"
                        ).strip(),
                        "sizes": _strings(raw.get("capabilities"), "sizes"),
                        "qualities": _strings(raw.get("capabilities"), "qualities"),
                        "formats": _strings(raw.get("capabilities"), "formats"),
                    },
                    "limits": {
                        "max_concurrency": _nested(raw, "limits", "max_concurrency"),
                        "timeout_seconds": _nested(raw, "limits", "timeout_seconds"),
                        "estimated_seconds": _nested(raw, "limits", "estimated_seconds"),
                    },
                }
            )
        queue = payload.get("queue")
        if not isinstance(queue, dict):
            raise ServiceError("队列配置格式无效")
        return {
            "version": 1,
            "queue": {
                "global_concurrency": queue.get("global_concurrency"),
                "max_queued_per_user": queue.get("max_queued_per_user"),
                "max_queued_global": queue.get("max_queued_global"),
                "history_retention_days": queue.get("history_retention_days"),
                "stale_running_minutes": queue.get("stale_running_minutes"),
            },
            "channels": channels,
        }

    def _chat_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ServiceError("对话模型列表格式无效")
        existing = {model.identifier: model for model in self.chat_models.list()}
        models: list[dict[str, Any]] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                raise ServiceError("对话模型条目格式无效")
            identifier = str(raw.get("id", "")).strip()
            old = existing.get(identifier)
            models.append(
                {
                    "id": identifier,
                    "label": str(raw.get("label", "")).strip(),
                    "enabled": as_bool(raw.get("enabled", True)),
                    "base_url": str(raw.get("base_url", "")).strip(),
                    "api_key": _resolved_key(raw, old.api_key if old else ""),
                    "model": str(raw.get("model", "")).strip(),
                    "reasoning_effort": str(raw.get("reasoning_effort", "")).strip(),
                    "timeout_seconds": raw.get("timeout_seconds"),
                    "max_output_tokens": raw.get("max_output_tokens"),
                }
            )
        context = payload.get("context")
        if not isinstance(context, dict):
            raise ServiceError("上下文配置格式无效")
        return {
            "version": 1,
            "prompt_draft_model_id": str(payload.get("prompt_draft_model_id", "")).strip(),
            "context": {
                "compact_at_tokens": context.get("compact_at_tokens"),
                "max_context_tokens": context.get("max_context_tokens"),
                "keep_recent_messages": context.get("keep_recent_messages"),
            },
            "models": models,
        }

    @staticmethod
    def _guard_active_channels(snapshot: ChannelSnapshot) -> None:
        active_ids = set(
            db.session.scalars(
                select(GenerationItem.channel_id)
                .where(GenerationItem.status.in_(ACTIVE_GENERATION_STATUSES))
                .distinct()
            )
        )
        unavailable = [
            identifier
            for identifier in active_ids
            if identifier not in snapshot.channels or not snapshot.channels[identifier].configured
        ]
        if unavailable:
            raise ServiceError(
                f"渠道 {', '.join(sorted(unavailable))} 仍有生成任务，暂时不能停用或删除",
                code="channel_in_use",
                status_code=409,
            )


def _models(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ServiceError("渠道模型列表格式无效")
    models: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ServiceError("渠道模型条目格式无效")
        models.append(
            {
                "id": str(item.get("id", "")).strip(),
                "label": str(item.get("label", "")).strip(),
                "enabled": as_bool(item.get("enabled", True)),
            }
        )
    return models


def _strings(value: Any, key: str) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise ServiceError(f"配置字段 {key} 格式无效")
    return [str(item).strip() for item in value[key]]


def _nested(value: dict[str, Any], group: str, key: str) -> Any:
    nested = value.get(group)
    if not isinstance(nested, dict):
        raise ServiceError(f"配置分组 {group} 格式无效")
    return nested.get(key)


def _resolved_key(raw: dict[str, Any], existing: str) -> str:
    supplied = str(raw.get("api_key", "")).strip()
    if supplied:
        return supplied
    return "" if as_bool(raw.get("clear_api_key", False)) else existing
