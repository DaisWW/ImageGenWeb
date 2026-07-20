from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CreativeDirection:
    identifier: str
    label: str
    description: str
    guidance: tuple[str, ...]
    pitfalls: tuple[str, ...]
    required_fields: tuple[str, ...] = ()
    hard_checks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    identifier: str
    label: str
    direction_id: str
    styles: tuple[str, ...]
    scenes: tuple[str, ...]
    use_when: str
    guidance: tuple[str, ...]
    pitfalls: tuple[str, ...]
    example_case_ids: tuple[int, ...]
    case_refs: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    hard_checks: tuple[str, ...] = ()
