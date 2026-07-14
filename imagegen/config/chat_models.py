from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from ..validation import as_bool, bounded_int, required_string
from .base import ReloadableConfigRegistry


@dataclass(frozen=True)
class ContextPolicy:
    compact_at_tokens: int = 24000
    max_context_tokens: int = 32000
    keep_recent_messages: int = 12

    def as_dict(self) -> dict[str, int]:
        return {
            "compact_at_tokens": self.compact_at_tokens,
            "max_context_tokens": self.max_context_tokens,
            "keep_recent_messages": self.keep_recent_messages,
        }


@dataclass(frozen=True)
class ChatModelConfig:
    identifier: str
    label: str
    enabled: bool
    base_url: str
    model: str
    reasoning_effort: str
    timeout_seconds: int
    max_output_tokens: int
    api_key: str = field(repr=False)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.base_url) and bool(self.model)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }

    def editable_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class ChatModelSnapshot:
    version: str
    models: dict[str, ChatModelConfig]
    context: ContextPolicy


class ChatModelRegistry(ReloadableConfigRegistry[ChatModelSnapshot]):
    """Atomically reloads OpenAI-compatible chat models and keeps keys private."""

    READ_ERROR_PREFIX = "无法读取聊天模型配置"
    LOAD_ERROR_PREFIX = "聊天模型配置加载失败"
    NOT_LOADED_MESSAGE = "聊天模型配置尚未加载"

    @property
    def context(self) -> ContextPolicy:
        self.reload_if_changed()
        with self._lock:
            return self._require_snapshot().context

    def list(self) -> list[ChatModelConfig]:
        self.reload_if_changed()
        with self._lock:
            return list(self._require_snapshot().models.values())

    def get(self, identifier: str, *, require_available: bool = True) -> ChatModelConfig:
        self.reload_if_changed()
        with self._lock:
            model = self._require_snapshot().models.get(identifier)
        if model is None:
            raise ValueError(f"不支持的聊天模型：{identifier}")
        if require_available and not model.configured:
            raise ValueError(f"{model.label} 尚未配置 URL、模型或 API Key")
        return model

    def editable_config(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            snapshot = self._require_snapshot()
            return {
                "version": snapshot.version[:12],
                "source": self._source,
                "last_error": self._last_error,
                "models": [model.editable_dict() for model in snapshot.models.values()],
                "context": snapshot.context.as_dict(),
            }

    def _parse(self, raw: Any, raw_bytes: bytes) -> ChatModelSnapshot:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("对话模型配置必须包含 version: 1")
        context_raw = raw.get("context", {})
        if not isinstance(context_raw, dict):
            raise ValueError("context 配置必须是对象")
        context = ContextPolicy(
            compact_at_tokens=bounded_int(context_raw, "compact_at_tokens", 24000, 1000, 500000),
            max_context_tokens=bounded_int(context_raw, "max_context_tokens", 32000, 2000, 1000000),
            keep_recent_messages=bounded_int(context_raw, "keep_recent_messages", 12, 4, 100),
        )
        if context.compact_at_tokens >= context.max_context_tokens:
            raise ValueError("compact_at_tokens 必须小于 max_context_tokens")

        raw_models = raw.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise ValueError("聊天模型配置至少需要一个聊天模型")
        models: dict[str, ChatModelConfig] = {}
        for item in raw_models:
            model = self._parse_model(item)
            if model.identifier in models:
                raise ValueError(f"聊天模型 ID 重复：{model.identifier}")
            models[model.identifier] = model
        return ChatModelSnapshot(
            version=hashlib.sha256(raw_bytes).hexdigest(),
            models=models,
            context=context,
        )

    @staticmethod
    def _parse_model(raw: Any) -> ChatModelConfig:
        if not isinstance(raw, dict):
            raise ValueError("每个聊天模型配置必须是对象")
        identifier = required_string(raw, "id", 64, section="聊天模型")
        if not identifier.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"聊天模型 ID 无效：{identifier}")
        label = required_string(raw, "label", 100, section="聊天模型")
        base_url = os.environ.get(str(raw.get("base_url_env", "")).strip(), "").strip()
        base_url = (base_url or required_string(raw, "base_url", 500, section="聊天模型")).rstrip(
            "/"
        )
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{label} 的 base_url 必须是 HTTP(S) 地址")
        model = os.environ.get(str(raw.get("model_env", "")).strip(), "").strip()
        model = model or required_string(raw, "model", 150, section="聊天模型")
        reasoning_effort = os.environ.get(
            str(raw.get("reasoning_effort_env", "")).strip(), ""
        ).strip()
        reasoning_effort = reasoning_effort or str(raw.get("reasoning_effort", "")).strip()
        allowed_efforts = {"", "none", "minimal", "low", "medium", "high", "max"}
        if reasoning_effort not in allowed_efforts:
            raise ValueError(f"{label} 的 reasoning_effort 配置无效")
        api_key = str(raw.get("api_key", "")).strip()
        if not api_key:
            api_key = os.environ.get(str(raw.get("api_key_env", "")).strip(), "").strip()
        return ChatModelConfig(
            identifier=identifier,
            label=label,
            enabled=as_bool(raw.get("enabled", True)),
            base_url=base_url,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=bounded_int(raw, "timeout_seconds", 180, 10, 600),
            max_output_tokens=bounded_int(raw, "max_output_tokens", 2000, 128, 16000),
            api_key=api_key,
        )
