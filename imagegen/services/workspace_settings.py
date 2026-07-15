from __future__ import annotations

from typing import Any

from ..errors import ServiceError
from ..validation import as_bool
from .common import normalize_image_size
from .settings import RuntimeSettings

ALLOWED_WORKSPACE_SETTING_KEYS = {
    "auto_title",
    "chat_model_id",
    "translate_prompt",
    "mode",
    "prompt",
    "channel_id",
    "model",
    "size",
    "quality",
    "output_format",
    "compression",
    "transparent_background",
    "batch_count",
    "animation_frame_count",
    "animation_fps",
    "animation_loop",
    "animation_format",
}


def default_workspace_settings(workspace_kind: str = "image") -> dict[str, Any]:
    return {
        "auto_title": True,
        "chat_model_id": "",
        "translate_prompt": False,
        "mode": "text2img",
        "prompt": "",
        "channel_id": "",
        "model": "",
        "size": "1024x1024",
        "quality": "auto",
        "output_format": "png",
        "compression": 90,
        "transparent_background": workspace_kind == "animation",
        "batch_count": 1,
        "animation_frame_count": 8,
        "animation_fps": 8,
        "animation_loop": True,
        "animation_format": "webp",
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
    settings["auto_title"] = as_bool(settings["auto_title"])
    settings["chat_model_id"] = str(settings["chat_model_id"])[:64]
    settings["translate_prompt"] = as_bool(settings["translate_prompt"])
    settings["mode"] = str(settings["mode"])
    settings["channel_id"] = str(settings["channel_id"])[:64]
    settings["model"] = str(settings["model"])[:100]
    settings["size"] = normalize_image_size(settings["size"])
    settings["quality"] = str(settings["quality"])[:20]
    settings["output_format"] = str(settings["output_format"])[:20]
    settings["transparent_background"] = as_bool(settings["transparent_background"])
    settings["animation_loop"] = as_bool(settings["animation_loop"])
    if settings["output_format"] not in {"png", "webp"}:
        settings["transparent_background"] = False
    try:
        settings["compression"] = min(100, max(0, int(settings["compression"])))
        settings["batch_count"] = min(
            runtime.max_batch_images, max(1, int(settings["batch_count"]))
        )
        settings["animation_frame_count"] = min(
            runtime.max_animation_frames, max(2, int(settings["animation_frame_count"]))
        )
        settings["animation_fps"] = min(
            runtime.max_animation_fps, max(1, int(settings["animation_fps"]))
        )
    except (TypeError, ValueError) as exc:
        raise ServiceError("工作站数字参数无效") from exc
    settings["animation_format"] = str(settings["animation_format"]).lower()
    if settings["animation_format"] not in {"webp", "gif"}:
        settings["animation_format"] = "webp"
    return settings
