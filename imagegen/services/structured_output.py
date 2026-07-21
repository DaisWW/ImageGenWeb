from __future__ import annotations

import json
from typing import Any


def parse_json_object(content: str) -> dict[str, Any] | None:
    """Parse one unambiguous JSON object from an AI text response."""
    if not isinstance(content, str):
        return None
    cleaned = content.strip()
    if not cleaned:
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    try:
        payload = decoder.decode(cleaned)
    except (TypeError, ValueError):
        start = cleaned.find("{")
        if start < 0 or cleaned[:start].rstrip().endswith(("[", ",")):
            return None
        try:
            payload, end = decoder.raw_decode(cleaned, start)
        except (TypeError, ValueError):
            return None
        if "{" in cleaned[end:]:
            return None
    return payload if isinstance(payload, dict) else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
