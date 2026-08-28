from __future__ import annotations

import copy
import hashlib
import math
import os
from dataclasses import dataclass, field
from typing import Any

from ..validation import as_bool, bounded_int, required_string
from .base import ReloadableConfigRegistry

ADAPTER_ALIASES = {
    "lucida": "lucida",
    "lucida_http": "lucida",
    "lucida-compatible": "lucida",
    "lucida_compatible": "lucida",
    "chroma-key": "chroma_key",
    "chroma": "chroma_key",
    "chroma_key": "chroma_key",
    "local-chroma": "chroma_key",
    "local_chroma": "chroma_key",
    "two-pass": "two_pass_chroma",
    "two_pass": "two_pass_chroma",
    "two_pass_chroma": "two_pass_chroma",
    "hybrid": "two_pass_chroma",
    "generic_http": "generic_http",
    "generic-http": "generic_http",
    "http": "generic_http",
    "rembg": "generic_http",
    "rembg_http": "generic_http",
    "birefnet": "generic_http",
    "birefnet_http": "generic_http",
    "rmbg": "generic_http",
    "rmbg_http": "generic_http",
    "inspyrenet": "generic_http",
    "inspyrenet_http": "generic_http",
}
SUPPORTED_ADAPTERS = frozenset(ADAPTER_ALIASES.values())
LOCAL_ADAPTERS = frozenset({"chroma_key", "two_pass_chroma"})
_CHROMA_OPTION_KEYS = frozenset(
    {
        "profile",
        "algorithm",
        "key_color",
        "threshold",
        "similarity",
        "softness",
        "despill",
        "despill_strength",
        "edge_feather",
        "feather",
        "preserve_alpha",
        "border_sample",
        "object",
        "particle",
    }
)
_HTTP_OPTION_KEYS = frozenset(
    {
        "remove_path",
        "health_path",
        "model_param",
        "file_field",
        "response_field",
        "decontaminate",
    }
)


@dataclass(frozen=True)
class MattingModelConfig:
    identifier: str
    label: str
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: int
    max_concurrency: int
    adapter_id: str = "lucida"
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", copy.deepcopy(dict(self.options or {})))

    @property
    def adapter(self) -> str:
        return self.adapter_id

    @property
    def backend(self) -> str:
        return self.adapter_id

    @property
    def configured(self) -> bool:
        if not self.enabled:
            return False
        if self.adapter_id in LOCAL_ADAPTERS:
            return True
        return bool(self.base_url) and bool(self.model)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "adapter_id": self.adapter_id,
            "backend": self.adapter_id,
            "model": self.model,
            "max_concurrency": self.max_concurrency,
            "options": copy.deepcopy(self.options),
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
        adapter_values = [
            raw.get(name)
            for name in ("adapter_id", "adapter", "backend")
            if raw.get(name) not in (None, "")
        ]
        adapter_id = normalize_adapter_id(adapter_values[0] if adapter_values else "lucida")
        if any(normalize_adapter_id(value) != adapter_id for value in adapter_values[1:]):
            raise ValueError(f"{label} 的 adapter_id、adapter、backend 配置不一致")
        base_url = os.environ.get(str(raw.get("base_url_env", "")).strip(), "").strip()
        base_url = (base_url or str(raw.get("base_url", "")).strip()).rstrip("/")
        if len(base_url) > 500:
            raise ValueError(f"{label} 的 base_url 不能超过 500 个字符")
        if base_url and not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{label} 的 base_url 必须是 HTTP(S) 地址")
        model = os.environ.get(str(raw.get("model_env", "")).strip(), "").strip()
        model = model or str(raw.get("model", "")).strip()
        if not model:
            if adapter_id in LOCAL_ADAPTERS:
                model = "balanced"
            elif adapter_id == "generic_http":
                model = "default"
            else:
                raise ValueError(f"{label} 缺少 model")
        if len(model) > 150:
            raise ValueError(f"{label} 的 model 不能超过 150 个字符")
        options = _parse_options(raw.get("options"), adapter_id, label)
        for key in _CHROMA_OPTION_KEYS | _HTTP_OPTION_KEYS:
            if key in raw and key not in options:
                options[key] = raw[key]
        options = _parse_options(options, adapter_id, label)
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
            adapter_id=adapter_id,
            options=options,
        )


def normalize_adapter_id(value: Any) -> str:
    identifier = str(value or "").strip().lower().replace(" ", "_")
    identifier = ADAPTER_ALIASES.get(identifier, identifier)
    if identifier not in SUPPORTED_ADAPTERS:
        supported = ", ".join(sorted(SUPPORTED_ADAPTERS))
        raise ValueError(f"不支持的透明化适配器：{identifier}（可选：{supported}）")
    return identifier


def _parse_options(value: Any, adapter_id: str, label: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} 的 options 必须是对象")
    if len(value) > 24:
        raise ValueError(f"{label} 的 options 不能超过 24 个字段")
    allowed = _CHROMA_OPTION_KEYS if adapter_id in LOCAL_ADAPTERS else _HTTP_OPTION_KEYS
    options: dict[str, Any] = {}
    for key, raw_value in value.items():
        name = str(key).strip()
        if name not in allowed:
            raise ValueError(f"{label} 不支持透明化参数：{name}")
        options[name] = _validate_option(name, raw_value, adapter_id, label)
    return options


def _validate_option(name: str, value: Any, adapter_id: str, label: str) -> Any:
    if name in {"threshold", "similarity", "softness", "despill", "despill_strength"}:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} 的 {name} 必须是数字") from exc
        if not math.isfinite(numeric) or not 0 <= numeric <= 1:
            raise ValueError(f"{label} 的 {name} 必须在 0 到 1 之间")
        return numeric
    if name in {"edge_feather", "feather", "border_sample"}:
        if isinstance(value, bool):
            raise ValueError(f"{label} 的 {name} 必须是整数")
        try:
            numeric = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} 的 {name} 必须是整数") from exc
        if isinstance(value, float) and value != numeric:
            raise ValueError(f"{label} 的 {name} 必须是整数")
        limits = {"edge_feather": (0, 8), "feather": (0, 8), "border_sample": (4, 128)}
        minimum, maximum = limits[name]
        if numeric < minimum or numeric > maximum:
            raise ValueError(f"{label} 的 {name} 必须在 {minimum} 到 {maximum} 之间")
        return numeric
    if name == "key_color":
        if isinstance(value, str):
            text = value.strip().lstrip("#")
            if len(text) != 6 or any(
                character not in "0123456789abcdefABCDEF" for character in text
            ):
                raise ValueError(f"{label} 的 key_color 必须是 #RRGGBB")
            return value.strip()
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                values = [int(item) for item in value]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label} 的 key_color 无效") from exc
            if any(item < 0 or item > 255 for item in values):
                raise ValueError(f"{label} 的 key_color 必须在 0 到 255 之间")
            return values
        raise ValueError(f"{label} 的 key_color 必须是 #RRGGBB 或三个整数")
    if name in {"preserve_alpha", "decontaminate"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "0",
            "false",
            "no",
            "off",
        }:
            return value.strip().lower() in {"1", "true", "yes", "on"}
        raise ValueError(f"{label} 的 {name} 必须是布尔值")
    if name in {
        "profile",
        "algorithm",
        "model_param",
        "file_field",
        "response_field",
        "remove_path",
        "health_path",
    }:
        text = str(value).strip()
        optional = name in {"model_param", "response_field", "health_path"}
        if not text and optional:
            return ""
        if not text or len(text) > 120:
            raise ValueError(f"{label} 的 {name} 无效")
        if name in {"remove_path", "health_path"} and not text.startswith("/"):
            raise ValueError(f"{label} 的 {name} 必须以 / 开头")
        if name in {"model_param", "file_field", "response_field"} and not all(
            character.isalnum() or character in "_-." for character in text
        ):
            raise ValueError(f"{label} 的 {name} 包含无效字符")
        if name in {"profile", "algorithm"} and adapter_id in LOCAL_ADAPTERS:
            if text.lower() not in {"balanced", "clean", "soft"}:
                raise ValueError(f"{label} 的 {name} 必须是 balanced、clean 或 soft")
            return text.lower()
        return text
    if name in {"object", "particle"}:
        if adapter_id != "two_pass_chroma" or not isinstance(value, dict):
            raise ValueError(f"{label} 的 {name} 必须是对象")
        return _parse_options(value, "chroma_key", label)
    return value
