from __future__ import annotations


def api_key_hint(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}****{value[-4:]}"
