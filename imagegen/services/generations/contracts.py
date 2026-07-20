from __future__ import annotations

from dataclasses import dataclass, field

from ...errors import ServiceError
from ..creative import get_creative_direction

GENERATION_STAGE_QUALITY = {"draft": "low", "refine": "medium", "final": "high"}
GENERATION_QUALITIES = set(GENERATION_STAGE_QUALITY.values())


@dataclass(frozen=True)
class SubmitGeneration:
    channel_id: str
    model: str
    mode: str
    prompt: str
    size: str
    output_format: str
    compression: int
    batch_count: int
    reference_ids: tuple[str, ...]
    quality: str = "high"
    workflow: dict[str, object] = field(default_factory=dict)
    transparent_background: bool = False


@dataclass(frozen=True, slots=True)
class GenerationWorkflow:
    quality: str
    metadata: dict[str, object]

    @classmethod
    def build(
        cls,
        *,
        stage: str,
        prompt_draft_id: str,
        draft: dict[str, object] | None,
        creative_direction_id: str,
    ) -> GenerationWorkflow:
        normalized_stage = str(stage).strip().lower()
        if normalized_stage not in GENERATION_STAGE_QUALITY:
            raise ServiceError("生成阶段无效")

        requested_direction_id = str(
            draft.get("creative_direction", "other") if draft is not None else creative_direction_id
        )
        try:
            direction = get_creative_direction(requested_direction_id)
        except ValueError:
            direction = get_creative_direction("other")
        direction_id = direction.identifier if direction else "auto"
        metadata = {
            "prompt_draft_id": prompt_draft_id,
            "creative_direction_id": direction_id,
            "creative_direction_label": direction.label if direction else "用户直接提示词",
            "template_id": str(draft.get("template_id", "custom")) if draft else "custom",
            "template_label": (
                str(draft.get("template_label", "自定义 Craft")) if draft else "用户直接提示词"
            ),
            "style_tags": draft.get("style_tags", []) if draft else [],
            "scene_tags": draft.get("scene_tags", []) if draft else [],
            "selection_reason": str(draft.get("selection_reason", "")) if draft else "",
            "case_refs": draft.get("case_refs", []) if draft else [],
            "template_required_fields": (
                draft.get("template_required_fields", []) if draft else []
            ),
            "template_hard_checks": draft.get("template_hard_checks", []) if draft else [],
            "brief": draft.get("brief", {}) if draft else {},
            "production_spec": draft.get("production_spec", {}) if draft else {},
            "generation_stage": normalized_stage,
            "ai_reviewed": draft is not None,
            "hard_checks": draft.get("hard_checks", []) if draft else [],
            "sources": draft.get("sources", []) if draft else [],
        }
        return cls(
            quality=GENERATION_STAGE_QUALITY[normalized_stage],
            metadata=metadata,
        )


def sanitize_workflow(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "prompt_draft_id",
        "creative_direction_id",
        "creative_direction_label",
        "template_id",
        "template_label",
        "style_tags",
        "scene_tags",
        "selection_reason",
        "case_refs",
        "template_required_fields",
        "template_hard_checks",
        "brief",
        "production_spec",
        "generation_stage",
        "ai_reviewed",
        "hard_checks",
        "sources",
    }
    result = {key: value[key] for key in allowed if key in value}
    result["prompt_draft_id"] = str(result.get("prompt_draft_id", "")).strip().lower()[:32]
    result["creative_direction_id"] = (
        str(result.get("creative_direction_id", "auto")).strip().lower()[:40]
    )
    result["creative_direction_label"] = str(result.get("creative_direction_label", ""))[:100]
    result["template_id"] = str(result.get("template_id", "custom")).strip().lower()[:80]
    result["template_label"] = str(result.get("template_label", "自定义 Craft"))[:120]
    result["selection_reason"] = str(result.get("selection_reason", ""))[:500]
    for key in ("style_tags", "scene_tags"):
        tags = result.get(key)
        result[key] = [str(item)[:80] for item in tags[:4]] if isinstance(tags, list) else []
    for key in ("case_refs", "template_required_fields", "template_hard_checks"):
        values = result.get(key)
        result[key] = [str(item)[:160] for item in values[:12]] if isinstance(values, list) else []
    stage = str(result.get("generation_stage", "final")).lower()
    result["generation_stage"] = stage if stage in {"draft", "refine", "final"} else "final"
    result["ai_reviewed"] = result.get("ai_reviewed") is True
    checks = result.get("hard_checks")
    result["hard_checks"] = (
        [str(item)[:300] for item in checks[:6]] if isinstance(checks, list) else []
    )
    sources = result.get("sources")
    result["sources"] = sources[:3] if isinstance(sources, list) else []
    result["brief"] = _sanitize_workflow_mapping(result.get("brief"))
    result["production_spec"] = _sanitize_workflow_mapping(result.get("production_spec"))
    return result


def _sanitize_workflow_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key, raw in list(value.items())[:40]:
        name = str(key).strip()[:80]
        if not name:
            continue
        sanitized = _sanitize_workflow_value(raw)
        if sanitized is not None:
            result[name] = sanitized
    return result


def _sanitize_workflow_value(value: object, depth: int = 0) -> object | None:
    if depth > 2:
        return str(value)[:300] if isinstance(value, str) else None
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, raw in list(value.items())[:20]:
            name = str(key).strip()[:80]
            if not name:
                continue
            sanitized = _sanitize_workflow_value(raw, depth + 1)
            if sanitized is not None:
                result[name] = sanitized
        return result
    if isinstance(value, list):
        return [
            sanitized
            for item in value[:12]
            if (sanitized := _sanitize_workflow_value(item, depth + 1)) is not None
        ]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)):
        return value
    return None
