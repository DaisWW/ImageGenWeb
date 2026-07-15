from __future__ import annotations

import hashlib
from typing import Any

_MISSING = object()


def response_summary(response: Any, payload: Any = _MISSING) -> dict[str, Any]:
    """描述上游响应，但不保留提示词或生成内容。"""
    headers = getattr(response, "headers", {}) or {}
    summary: dict[str, Any] = {
        "status_code": getattr(response, "status_code", None),
        "content_type": str(headers.get("content-type", ""))[:120],
    }
    raw = getattr(response, "content", None)
    if isinstance(raw, bytes):
        summary["body_bytes"] = len(raw)
        summary["body_sha256"] = hashlib.sha256(raw).hexdigest()
    elif str(headers.get("content-length", "")).isdigit():
        summary["body_bytes"] = int(headers["content-length"])

    if payload is _MISSING:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            summary["json_type"] = "invalid"
            return summary
    summary.update(_payload_shape(payload))
    return summary


def _payload_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"json_type": type(payload).__name__}

    summary: dict[str, Any] = {
        "json_type": "object",
        "top_level_keys": sorted(str(key)[:80] for key in payload)[:50],
    }
    for key in ("code", "type"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            summary[key] = str(value)[:120]

    error = payload.get("error")
    if isinstance(error, dict):
        summary["error_keys"] = sorted(str(key)[:80] for key in error)[:30]
        for key in ("code", "type"):
            value = error.get(key)
            if isinstance(value, (str, int)):
                summary[f"error_{key}"] = str(value)[:120]

    choices = payload.get("choices")
    if isinstance(choices, list):
        summary["choices_count"] = len(choices)
        if choices and isinstance(choices[0], dict):
            summary["first_choice_keys"] = sorted(str(key)[:80] for key in choices[0])[:30]
            message = choices[0].get("message")
            if isinstance(message, dict):
                summary["message_keys"] = sorted(str(key)[:80] for key in message)[:30]
                summary["message_content_type"] = type(message.get("content")).__name__
            else:
                summary["message_type"] = type(message).__name__
    elif choices is not None:
        summary["choices_type"] = type(choices).__name__

    for key in ("output", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
            if value and isinstance(value[0], dict):
                summary[f"first_{key}_keys"] = sorted(str(item)[:80] for item in value[0])[:30]
        elif value is not None:
            summary[f"{key}_type"] = type(value).__name__
    return summary
