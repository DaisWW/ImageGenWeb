from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from ..validation import as_bool, bounded_int, required_string
from .base import ReloadableConfigRegistry

SUPPORTED_ADAPTERS = {"openai_images"}
SUPPORTED_MODES = {"text2img", "img2img"}
SUPPORTED_FORMATS = {"png", "jpeg", "webp"}
SUPPORTED_QUALITIES = {"auto", "low", "medium", "high"}


@dataclass(frozen=True)
class ChannelCapabilities:
    modes: tuple[str, ...]
    max_reference_images: int
    max_reference_image_mb: int
    max_reference_total_mb: int
    sizes: tuple[str, ...]
    qualities: tuple[str, ...]
    formats: tuple[str, ...]


@dataclass(frozen=True)
class ChannelLimits:
    max_concurrency: int
    timeout_seconds: int
    estimated_seconds: int


@dataclass(frozen=True)
class ChannelModel:
    identifier: str
    label: str
    enabled: bool = True


@dataclass(frozen=True)
class Channel:
    identifier: str
    label: str
    enabled: bool
    adapter: str
    base_url: str
    models: tuple[ChannelModel, ...]
    price_rmb: Decimal
    capabilities: ChannelCapabilities
    limits: ChannelLimits
    api_key: str = field(repr=False)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    @property
    def default_model(self) -> ChannelModel:
        return next(model for model in self.models if model.enabled)

    def get_model(self, identifier: str) -> ChannelModel:
        for model in self.models:
            if model.identifier == identifier and model.enabled:
                return model
        raise ValueError(f"{self.label} 不支持模型：{identifier}")

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "models": [
                {"id": model.identifier, "label": model.label}
                for model in self.models
                if model.enabled
            ],
            "default_model": self.default_model.identifier,
            "price_rmb": format(self.price_rmb, ".4f"),
            "capabilities": {
                "modes": list(self.capabilities.modes),
                "max_reference_images": self.capabilities.max_reference_images,
                "max_reference_image_mb": self.capabilities.max_reference_image_mb,
                "max_reference_total_mb": self.capabilities.max_reference_total_mb,
                "sizes": list(self.capabilities.sizes),
                "qualities": list(self.capabilities.qualities),
                "formats": list(self.capabilities.formats),
            },
            "limits": {"max_concurrency": self.limits.max_concurrency},
        }

    def editable_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "adapter": self.adapter,
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
            "models": [
                {
                    "id": model.identifier,
                    "label": model.label,
                    "enabled": model.enabled,
                }
                for model in self.models
            ],
            "price_rmb": format(self.price_rmb, ".4f"),
            "capabilities": {
                "modes": list(self.capabilities.modes),
                "max_reference_images": self.capabilities.max_reference_images,
                "max_reference_image_mb": self.capabilities.max_reference_image_mb,
                "max_reference_total_mb": self.capabilities.max_reference_total_mb,
                "sizes": list(self.capabilities.sizes),
                "qualities": list(self.capabilities.qualities),
                "formats": list(self.capabilities.formats),
            },
            "limits": {
                "max_concurrency": self.limits.max_concurrency,
                "timeout_seconds": self.limits.timeout_seconds,
                "estimated_seconds": self.limits.estimated_seconds,
            },
        }


@dataclass(frozen=True)
class QueueLimits:
    global_concurrency: int = 4
    max_queued_per_user: int = 20
    max_queued_global: int = 100
    history_retention_days: int = 30
    stale_running_minutes: int = 20

    def as_dict(self) -> dict[str, int]:
        return {
            "global_concurrency": self.global_concurrency,
            "max_queued_per_user": self.max_queued_per_user,
            "max_queued_global": self.max_queued_global,
            "history_retention_days": self.history_retention_days,
            "stale_running_minutes": self.stale_running_minutes,
        }


@dataclass(frozen=True)
class ChannelSnapshot:
    version: str
    channels: dict[str, Channel]
    queue: QueueLimits


class ChannelRegistry(ReloadableConfigRegistry[ChannelSnapshot]):
    """原子刷新已校验的渠道配置，并避免暴露密钥。"""

    READ_ERROR_PREFIX = "无法读取渠道配置"
    LOAD_ERROR_PREFIX = "渠道配置加载失败"
    NOT_LOADED_MESSAGE = "渠道配置尚未加载"

    @property
    def queue(self) -> QueueLimits:
        self.reload_if_changed()
        with self._lock:
            return self._require_snapshot().queue

    def list(self, *, include_disabled: bool = True) -> list[Channel]:
        self.reload_if_changed()
        with self._lock:
            channels = list(self._require_snapshot().channels.values())
        return (
            channels if include_disabled else [channel for channel in channels if channel.enabled]
        )

    def get(self, identifier: str, *, require_available: bool = True) -> Channel:
        self.reload_if_changed()
        with self._lock:
            channel = self._require_snapshot().channels.get(identifier)
        if channel is None:
            raise ValueError(f"不支持的生图渠道：{identifier}")
        if require_available and not channel.configured:
            raise ValueError(f"{channel.label} 渠道未启用或尚未配置 API Key")
        return channel

    def editable_config(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            snapshot = self._require_snapshot()
            return {
                "version": snapshot.version[:12],
                "source": self._source,
                "last_error": self._last_error,
                "queue": snapshot.queue.as_dict(),
                "channels": [channel.editable_dict() for channel in snapshot.channels.values()],
            }

    def _parse(self, raw: Any, raw_bytes: bytes) -> ChannelSnapshot:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("渠道配置必须包含 version: 1")
        queue = self._parse_queue(raw.get("queue", {}))
        raw_channels = raw.get("channels")
        if not isinstance(raw_channels, list) or not raw_channels:
            raise ValueError("渠道配置至少需要一个渠道")

        channels: dict[str, Channel] = {}
        for item in raw_channels:
            channel = self._parse_channel(item)
            if channel.identifier in channels:
                raise ValueError(f"渠道 ID 重复：{channel.identifier}")
            channels[channel.identifier] = channel
        version = hashlib.sha256(raw_bytes).hexdigest()
        return ChannelSnapshot(
            version=version,
            channels=channels,
            queue=queue,
        )

    def _parse_queue(self, raw: Any) -> QueueLimits:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("queue 配置必须是对象")
        queue = QueueLimits(
            global_concurrency=bounded_int(raw, "global_concurrency", 4, 1, 64),
            max_queued_per_user=bounded_int(raw, "max_queued_per_user", 20, 1, 500),
            max_queued_global=bounded_int(raw, "max_queued_global", 100, 1, 5000),
            history_retention_days=bounded_int(raw, "history_retention_days", 30, 1, 3650),
            stale_running_minutes=bounded_int(raw, "stale_running_minutes", 20, 5, 1440),
        )
        if queue.max_queued_global < queue.max_queued_per_user:
            raise ValueError("全局排队上限不能小于单用户排队上限")
        return queue

    def _parse_channel(self, raw: Any) -> Channel:
        if not isinstance(raw, dict):
            raise ValueError("每个渠道配置必须是对象")
        identifier = required_string(raw, "id", 64, section="渠道")
        if not identifier.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"渠道 ID 无效：{identifier}")
        label = required_string(raw, "label", 100, section="渠道")
        adapter = raw.get("adapter", "openai_images")
        if adapter not in SUPPORTED_ADAPTERS:
            raise ValueError(f"{label} 使用了不支持的适配器：{adapter}")
        base_url = os.environ.get(str(raw.get("base_url_env", "")).strip(), "").strip()
        base_url = (base_url or required_string(raw, "base_url", 500, section="渠道")).rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{label} 的 base_url 必须是 HTTP(S) 地址")
        models = self._parse_models(raw, label)
        try:
            price = Decimal(str(raw.get("price_rmb", "0"))).quantize(Decimal("0.0001"))
        except InvalidOperation as exc:
            raise ValueError(f"{label} 的 price_rmb 无效") from exc
        if price < 0:
            raise ValueError(f"{label} 的 price_rmb 不能为负数")

        capabilities_raw = raw.get("capabilities", {})
        if not isinstance(capabilities_raw, dict):
            raise ValueError(f"{label} 的 capabilities 必须是对象")
        modes = _string_tuple(capabilities_raw.get("modes", ["text2img"]), f"{label}.modes")
        if not modes or not set(modes) <= SUPPORTED_MODES:
            raise ValueError(f"{label} 的 modes 仅支持 text2img/img2img")
        formats = _string_tuple(capabilities_raw.get("formats", ["png"]), f"{label}.formats")
        qualities = _string_tuple(capabilities_raw.get("qualities", ["auto"]), f"{label}.qualities")
        sizes = _string_tuple(capabilities_raw.get("sizes", ["1024x1024"]), f"{label}.sizes")
        if not set(formats) <= SUPPORTED_FORMATS:
            raise ValueError(f"{label} 包含不支持的输出格式")
        if not set(qualities) <= SUPPORTED_QUALITIES:
            raise ValueError(f"{label} 包含不支持的质量参数")

        capabilities = ChannelCapabilities(
            modes=modes,
            max_reference_images=bounded_int(capabilities_raw, "max_reference_images", 1, 0, 8),
            max_reference_image_mb=bounded_int(
                capabilities_raw, "max_reference_image_mb", 10, 1, 50
            ),
            max_reference_total_mb=bounded_int(
                capabilities_raw, "max_reference_total_mb", 40, 1, 100
            ),
            sizes=sizes,
            qualities=qualities,
            formats=formats,
        )
        if "img2img" in modes and capabilities.max_reference_images < 1:
            raise ValueError(f"{label} 支持 img2img 时必须允许至少一张垫图")

        limits_raw = raw.get("limits", {})
        if not isinstance(limits_raw, dict):
            raise ValueError(f"{label} 的 limits 必须是对象")
        limits = ChannelLimits(
            max_concurrency=bounded_int(limits_raw, "max_concurrency", 2, 1, 64),
            timeout_seconds=bounded_int(limits_raw, "timeout_seconds", 600, 30, 1800),
            estimated_seconds=bounded_int(limits_raw, "estimated_seconds", 180, 10, 1800),
        )
        return Channel(
            identifier=identifier,
            label=label,
            enabled=as_bool(raw.get("enabled", True)),
            adapter=adapter,
            base_url=base_url,
            models=models,
            price_rmb=price,
            capabilities=capabilities,
            limits=limits,
            api_key=self._resolve_secret(raw),
        )

    @staticmethod
    def _parse_models(raw: dict[str, Any], label: str) -> tuple[ChannelModel, ...]:
        raw_models = raw.get("models")
        if raw_models is None and raw.get("model"):
            raw_models = [{"id": raw["model"], "label": raw["model"]}]
        if not isinstance(raw_models, list) or not raw_models:
            raise ValueError(f"{label} 至少需要配置一个模型")
        models: list[ChannelModel] = []
        identifiers: set[str] = set()
        for item in raw_models:
            if isinstance(item, str):
                item = {"id": item, "label": item}
            if not isinstance(item, dict):
                raise ValueError(f"{label} 的模型配置无效")
            identifier = required_string(item, "id", 100, section="渠道")
            model_label = str(item.get("label", identifier)).strip()
            if not model_label or len(model_label) > 100:
                raise ValueError(f"{label} 的模型名称无效")
            if identifier in identifiers:
                raise ValueError(f"{label} 的模型 ID 重复：{identifier}")
            identifiers.add(identifier)
            models.append(
                ChannelModel(
                    identifier=identifier,
                    label=model_label,
                    enabled=as_bool(item.get("enabled", True)),
                )
            )
        if not any(model.enabled for model in models):
            raise ValueError(f"{label} 至少需要启用一个模型")
        return tuple(models)

    def _resolve_secret(self, raw: dict[str, Any]) -> str:
        direct = str(raw.get("api_key", "")).strip()
        if direct:
            return direct
        env_name = str(raw.get("api_key_env", "")).strip()
        if env_name:
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        secret_file = str(raw.get("api_key_file", "")).strip()
        if not secret_file:
            return ""
        path = Path(secret_file)
        if not path.is_absolute():
            path = self._path.parent / path
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _file_signature(self, raw: dict[str, Any] | None = None) -> tuple:
        stat = self._path.stat()
        signature: list[Any] = [stat.st_mtime_ns, stat.st_size]
        if raw is None:
            raw = yaml.safe_load(self._path.read_bytes()) or {}
        for item in raw.get("channels", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict) or not item.get("api_key_file"):
                continue
            path = Path(str(item["api_key_file"]))
            if not path.is_absolute():
                path = self._path.parent / path
            try:
                secret_stat = path.stat()
                signature.extend((str(path), secret_stat.st_mtime_ns, secret_stat.st_size))
            except OSError:
                signature.extend((str(path), None, None))
        return tuple(signature)


def _string_tuple(raw: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ValueError(f"{field_name} 必须是非空字符串列表")
    return tuple(dict.fromkeys(item.strip().lower() for item in raw))
