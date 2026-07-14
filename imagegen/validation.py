from __future__ import annotations

from typing import Any


def as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def bounded_int(raw: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置字段 {key} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"配置字段 {key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def required_string(raw: dict[str, Any], key: str, max_length: int, *, section: str) -> str:
    value = str(raw.get(key, "")).strip()
    if not value or len(value) > max_length or "\n" in value:
        raise ValueError(f"{section}字段 {key} 无效")
    return value
