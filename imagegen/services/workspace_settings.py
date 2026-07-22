from __future__ import annotations

from typing import Any

from ..errors import ServiceError
from ..validation import as_bool
from .common import normalize_image_size
from .series import SeriesAnchor
from .settings import RuntimeSettings

ALLOWED_WORKSPACE_SETTING_KEYS = {
    "chat_model_id",
    "translate_prompt",
    "creative_direction_id",
    "prompt_draft_id",
    "generation_stage",
    "reference_ids",
    "mode",
    "prompt",
    "channel_id",
    "model",
    "size",
    "output_format",
    "compression",
    "transparent_background",
    "batch_count",
    "generation_strategy",
    "series_anchor",
}


def default_workspace_settings() -> dict[str, Any]:
    return {
        "chat_model_id": "",
        "translate_prompt": False,
        "creative_direction_id": "auto",
        "prompt_draft_id": "",
        "generation_stage": "draft",
        "mode": "text2img",
        "prompt": "",
        "channel_id": "",
        "model": "",
        "size": "1024x1024",
        "output_format": "png",
        "compression": 90,
        "transparent_background": False,
        "batch_count": 1,
        "generation_strategy": "sample",
        "series_anchor": {},
    }


def sanitize_workspace_settings(raw: Any, runtime: RuntimeSettings | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ServiceError("工作站参数格式无效")
    runtime = runtime or RuntimeSettings()
    settings = default_workspace_settings()
    for key in ALLOWED_WORKSPACE_SETTING_KEYS:
        if key in raw:
            settings[key] = raw[key]
    settings["prompt"] = str(settings["prompt"])[: runtime.max_prompt_characters]
    settings["chat_model_id"] = str(settings["chat_model_id"])[:64]
    settings["translate_prompt"] = as_bool(settings["translate_prompt"])
    settings["creative_direction_id"] = str(settings["creative_direction_id"])[:40]
    settings["prompt_draft_id"] = str(settings["prompt_draft_id"])[:32]
    settings["generation_stage"] = str(settings["generation_stage"]).lower()
    if settings["generation_stage"] not in {"draft", "refine", "final"}:
        settings["generation_stage"] = "draft"
    settings["generation_strategy"] = str(settings["generation_strategy"]).lower()
    if settings["generation_strategy"] not in {"sample", "explore", "series"}:
        settings["generation_strategy"] = "sample"
    series_anchor = SeriesAnchor.parse(settings.get("series_anchor"))
    settings["series_anchor"] = series_anchor.as_dict() if series_anchor else {}
    if "reference_ids" in raw:
        if not isinstance(settings["reference_ids"], list):
            raise ServiceError("垫图选择参数无效")
        settings["reference_ids"] = [
            str(item)[:32] for item in settings["reference_ids"][: runtime.max_assets_per_workspace]
        ]
    settings["mode"] = str(settings["mode"])
    settings["channel_id"] = str(settings["channel_id"])[:64]
    settings["model"] = str(settings["model"])[:100]
    settings["size"] = normalize_image_size(settings["size"])
    settings["output_format"] = str(settings["output_format"])[:20]
    settings["transparent_background"] = as_bool(settings["transparent_background"])
    if settings["output_format"] not in {"png", "webp"}:
        settings["transparent_background"] = False
    try:
        settings["compression"] = min(100, max(0, int(settings["compression"])))
        settings["batch_count"] = min(
            runtime.max_batch_images, max(1, int(settings["batch_count"]))
        )
    except (TypeError, ValueError) as exc:
        raise ServiceError("工作站数字参数无效") from exc
    return settings
