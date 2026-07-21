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
    gallery_categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GalleryCategory:
    identifier: str
    label: str
    case_start: int
    case_end: int
    direction_ids: tuple[str, ...]
    prompt_schema: str

    @property
    def source_file(self) -> str:
        return f"gallery-{self.identifier}.md"

    @property
    def case_ref(self) -> str:
        return f"skill:{self.case_start}-{self.case_end}"


@dataclass(frozen=True, slots=True)
class CreativeCase:
    identifier: str
    source: str
    title: str
    prompt: str
    category: str
    direction_id: str
    source_url: str
    gallery_category: str = ""
    styles: tuple[str, ...] = ()
    scenes: tuple[str, ...] = ()
    attribution: str = ""


@dataclass(frozen=True, slots=True)
class EditRecipe:
    identifier: str
    label: str
    description: str
    prompt_schema: str
    required_fields: tuple[str, ...]
    hard_checks: tuple[str, ...]
