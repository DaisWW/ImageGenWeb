from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

from ..validation import as_bool, bounded_int, required_string
from .base import ReloadableConfigRegistry


@dataclass(frozen=True)
class MattingModelConfig:
    identifier: str
    label: str
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: int
    max_concurrency: int

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.base_url) and bool(self.model)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "model": self.model,
            "max_concurrency": self.max_concurrency,
        }

    def editable_dict(self) -> dict[str, Any]:
        return {
            **self.public_dict(),
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class MattingModelSnapshot:
    version: str
    models: dict[str, MattingModelConfig]


class MattingModelRegistry(ReloadableConfigRegistry[MattingModelSnapshot]):
    """Ordered background-removal model configuration."""

    READ_ERROR_PREFIX = "无法读取透明化模型配置"
    LOAD_ERROR_PREFIX = "透明化模型配置加载失败"
    NOT_LOADED_MESSAGE = "透明化模型配置尚未加载"

    def list(self, *, include_disabled: bool = False) -> list[MattingModelConfig]:
        self.reload_if_changed()
        with self._lock:
            models = list(self._require_snapshot().models.values())
        if include_disabled:
            return models
        return [model for model in models if model.enabled]

    def get(self, identifier: str, *, require_available: bool = True) -> MattingModelConfig:
        self.reload_if_changed()
        with self._lock:
            model = self._require_snapshot().models.get(identifier)
        if model is None:
            raise ValueError(f"不支持的透明化模型：{identifier}")
        if require_available and not model.configured:
            raise ValueError(f"{model.label} 尚未配置服务地址或上游模型")
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
            }

    def _parse(self, raw: Any, raw_bytes: bytes) -> MattingModelSnapshot:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("透明化模型配置必须包含 version: 1")
        raw_models = raw.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise ValueError("透明化模型配置至少需要一个模型")
        models: dict[str, MattingModelConfig] = {}
        for raw_model in raw_models:
            model = self._parse_model(raw_model)
            if model.identifier in models:
                raise ValueError(f"透明化模型 ID 重复：{model.identifier}")
            models[model.identifier] = model
        return MattingModelSnapshot(
            version=hashlib.sha256(raw_bytes).hexdigest(),
            models=models,
        )

    @staticmethod
    def _parse_model(raw: Any) -> MattingModelConfig:
        if not isinstance(raw, dict):
            raise ValueError("每个透明化模型配置必须是对象")
        identifier = required_string(raw, "id", 64, section="透明化模型")
        if not identifier.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"透明化模型 ID 无效：{identifier}")
        label = required_string(raw, "label", 100, section="透明化模型")
        base_url = os.environ.get(str(raw.get("base_url_env", "")).strip(), "").strip()
        base_url = (base_url or str(raw.get("base_url", "")).strip()).rstrip("/")
        if base_url and not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{label} 的 base_url 必须是 HTTP(S) 地址")
        model = os.environ.get(str(raw.get("model_env", "")).strip(), "").strip()
        model = model or required_string(raw, "model", 150, section="透明化模型")
        numeric = dict(raw)
        timeout_env = os.environ.get(str(raw.get("timeout_seconds_env", "")).strip(), "").strip()
        if timeout_env:
            numeric["timeout_seconds"] = timeout_env
        return MattingModelConfig(
            identifier=identifier,
            label=label,
            enabled=as_bool(raw.get("enabled", True)),
            base_url=base_url,
            model=model,
            timeout_seconds=bounded_int(numeric, "timeout_seconds", 120, 1, 1800),
            max_concurrency=bounded_int(raw, "max_concurrency", 1, 1, 8),
        )
