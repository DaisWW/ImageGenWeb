from .directions import CREATIVE_DIRECTIONS
from .models import CreativeDirection, PromptTemplate
from .templates import PROMPT_TEMPLATES, SCENE_TAG_LABELS, STYLE_TAG_LABELS

_DIRECTIONS_BY_ID = {direction.identifier: direction for direction in CREATIVE_DIRECTIONS}
_TEMPLATES_BY_ID = {template.identifier: template for template in PROMPT_TEMPLATES}


def creative_direction_dicts() -> list[dict[str, object]]:
    return [
        {
            "id": "auto",
            "label": "AI 自动匹配",
            "description": "按交付物、风格、场景和相近模板自动选择",
            "template_count": len(PROMPT_TEMPLATES),
        },
        *(
            {
                "id": direction.identifier,
                "label": direction.label,
                "description": direction.description,
                "template_count": sum(
                    template.direction_id == direction.identifier for template in PROMPT_TEMPLATES
                ),
            }
            for direction in CREATIVE_DIRECTIONS
        ),
    ]


def get_creative_direction(identifier: str) -> CreativeDirection | None:
    normalized = str(identifier or "auto").strip().lower()
    if normalized == "auto":
        return None
    try:
        return _DIRECTIONS_BY_ID[normalized]
    except KeyError as exc:
        raise ValueError("创作方向无效") from exc


def get_prompt_template(identifier: str) -> PromptTemplate | None:
    normalized = str(identifier or "custom").strip().lower()
    if normalized == "custom":
        return None
    return _TEMPLATES_BY_ID.get(normalized)


def normalize_template_id(identifier: object, direction_id: str) -> str:
    template = get_prompt_template(str(identifier or "custom"))
    return template.identifier if template and template.direction_id == direction_id else "custom"


def normalize_catalog_tags(value: object, *, scene: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = SCENE_TAG_LABELS if scene else STYLE_TAG_LABELS
    canonical = {name.lower(): name for name in labels}
    result = []
    for item in value:
        normalized = canonical.get(str(item).strip().lower())
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= 4:
            break
    return result


def catalog_tag_labels(tags: list[str], *, scene: bool = False) -> list[str]:
    labels = SCENE_TAG_LABELS if scene else STYLE_TAG_LABELS
    return [labels[tag] for tag in tags if tag in labels]


def creative_direction_prompt(identifier: str, *, include_templates: bool = True) -> str:
    direction = get_creative_direction(identifier)
    directions = CREATIVE_DIRECTIONS if direction is None else (direction,)
    options = "\n".join(
        f"- {item.identifier}: {item.label}；{item.description}" for item in directions
    )
    lock_rule = (
        "创作方向由 AI 自动匹配。"
        if direction is None
        else f"用户已锁定 `{direction.identifier}`（{direction.label}），不得改选其他方向。"
    )
    if not include_templates:
        return f"""{lock_rule}
当前是需求访谈，只按交付物识别方向并澄清会明显改变结果的关键分支；模板、风格、场景和 Case 将在系统自动整理最终提示词时统一筛选。
可选方向：
{options}
若用户从外部图库复制提示词，只提取可复用结构，以当前需求覆盖案例中的主体、文字、品牌和参考图职责。"""
    templates = "\n".join(
        _template_prompt_line(template)
        for template in PROMPT_TEMPLATES
        if direction is None or template.direction_id == direction.identifier
    )
    styles = "、".join(STYLE_TAG_LABELS)
    scenes = "、".join(SCENE_TAG_LABELS)
    return f"""{lock_rule}
必须按“交付物分类 → 视觉风格 → 使用场景 → 最近模板 → 相近 Case”完成筛选。若一个模板明显最匹配，直接采用；若 2～3 个模板会导致明显不同的交付结果，只提出一个包含这些模板及简短理由的选择题。没有近似模板时使用 `custom` 并遵循通用 Craft，禁止硬套模板。

可选方向：
{options}

awesome-gpt-image-2 模板目录：
{templates}

style_tags 只能从以下值选择：{styles}
scene_tags 只能从以下值选择：{scenes}
selection_reason 用一句中文说明为什么该方向/模板最符合交付物，不得声称读取过未提供的完整 Case 内容。
若用户从外部图库复制提示词，先提取画布、布局、风格、材质和约束等可复用结构，再以当前用户需求覆盖案例中的主体、文字、品牌和参考图职责。"""


def _template_prompt_line(template: PromptTemplate) -> str:
    cases = ",".join(str(case_id) for case_id in template.example_case_ids)
    return (
        f"- {template.identifier}｜{template.label}｜方向 {template.direction_id}｜"
        f"风格 {','.join(template.styles)}｜场景 {','.join(template.scenes)}｜"
        f"适用 {template.use_when}；重点 {' '.join(template.guidance)}；"
        f"避免 {' '.join(template.pitfalls)}；Case {cases}"
    )
