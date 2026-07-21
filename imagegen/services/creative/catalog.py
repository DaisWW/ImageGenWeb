from .directions import CREATIVE_DIRECTIONS
from .gallery import GALLERY_ATLAS
from .models import CreativeDirection, PromptTemplate
from .templates import PROMPT_TEMPLATES, SCENE_TAG_LABELS, STYLE_TAG_LABELS

_DIRECTIONS_BY_ID = {direction.identifier: direction for direction in CREATIVE_DIRECTIONS}
_TEMPLATES_BY_ID = {template.identifier: template for template in PROMPT_TEMPLATES}


def creative_direction_dicts() -> list[dict[str, object]]:
    return [
        {
            "id": "auto",
            "label": "AI 自动匹配",
            "description": "按交付物、Gallery、风格、场景和相近模板自动选择",
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
                "required_fields": list(direction.required_fields),
                "hard_checks": list(direction.hard_checks),
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
    options = "\n".join(_direction_prompt_line(item) for item in directions)
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
游戏任务路由：需要可玩的 HUD、菜单、背包、地图、任务或图标资产时使用 `game_ui`；需要 Key Art、角色、环境、武器、世界观板或动作分解时使用 `game_art`，不要用泛化的 `ui`、`character` 或 `scene` 替代。
游戏 UI 开发素材路由：当用户要求把完整界面变为 UI Kit、开发素材、可复用组件或进一步拆分时，使用 `game-ui-production-asset`。不得承诺从扁平截图抠图或无损拆层；先按“模块 → 原子资源”列出组件并让用户选择一个，本次未明确原子资源时必须继续澄清。
若用户从外部图库复制提示词，只提取可复用结构，以当前需求覆盖案例中的主体、文字、品牌和参考图职责。"""
    templates = "\n".join(
        _template_prompt_line(template)
        for template in PROMPT_TEMPLATES
        if direction is None or template.direction_id == direction.identifier
    )
    styles = "、".join(STYLE_TAG_LABELS)
    scenes = "、".join(SCENE_TAG_LABELS)
    direction_rules = "\n".join(
        (
            f"- {item.identifier} 的执行规则：{' '.join(item.guidance)}；"
            f"常见失误：{' '.join(item.pitfalls)}；"
            f"必填制作字段：{'、'.join(item.required_fields) or '按交付物补齐'}；"
            f"验收门槛：{'；'.join(item.hard_checks) or '按最终图片可判断项定义'}"
        )
        for item in directions
    )
    gallery_categories = GALLERY_ATLAS.prompt(direction.identifier if direction else None)
    return f"""{lock_rule}
必须按“交付物分类 → Gallery Atlas 类别 → 视觉风格 → 使用场景 → 最近模板 → 相近 Case”完成筛选。若一个模板明显最匹配，直接采用；若 2～3 个模板会导致明显不同的交付结果，只提出一个包含这些模板及简短理由的选择题。没有近似模板时使用 `custom` 并遵循通用 Craft，禁止硬套模板。

可选方向：
{options}

当前方向执行规则：
{direction_rules}

游戏方向特别规则：game_ui 与 game_art 必须分开。game_ui 先锁定平台、目标画布、屏幕状态和安全区，再定义 HUD/组件分区；game_art 先锁交付阶段、身份锚点、镜头动作、面板职责和跨面板一致性。案例编号只代表结构参考，不能声称读过案例正文，也不能复制案例中的 IP、角色、Logo 或文字。
game_ui 的开发组件必须与完整界面分开：选择 `game-ui-production-asset` 后，参考图只作结构与风格依据，禁止抠图、自动拆层、整屏重绘和 AI 排图集；一次只生成一个无文字、无动态数值的透明原子资源，运行时内容和九宫格要求写入 production_spec。

本地 Prompt 模板目录（融合 awesome-gpt-image-2 与 GPT-Image2-Skill 的结构参考）：
{templates}

Gallery Atlas 路由索引（31 类、162 个 Case 的本地结构摘要；以下不是完整案例正文）：
{gallery_categories}

gallery_categories 必须从上方 ID 中选择 1 个；只有两个或三个类别都不可替代时才选择 2～3 个。选定后必须实际使用对应“语法”组织提示词，不能只把类别名写进元数据。Case 范围仅用于来源追踪，不得声称读取过外部案例正文。

style_tags 只能从以下值选择：{styles}
scene_tags 只能从以下值选择：{scenes}
selection_reason 用一句中文说明为什么该方向/模板最符合交付物，不得声称读取过未提供的完整 Case 内容。
若用户从外部图库复制提示词，先提取画布、布局、风格、材质和约束等可复用结构，再以当前用户需求覆盖案例中的主体、文字、品牌和参考图职责。"""


def _template_prompt_line(template: PromptTemplate) -> str:
    cases = ",".join(template.case_refs) or ",".join(
        f"awesome:{case_id}" for case_id in template.example_case_ids
    )
    required = "、".join(template.required_fields) or "按交付物定义字段"
    checks = "；".join(template.hard_checks) or "按最终图片可判断项定义"
    return (
        f"- {template.identifier}｜{template.label}｜方向 {template.direction_id}｜"
        f"风格 {','.join(template.styles)}｜场景 {','.join(template.scenes)}｜"
        f"适用 {template.use_when}；重点 {' '.join(template.guidance)}；"
        f"避免 {' '.join(template.pitfalls)}；必填字段 {required}；"
        f"验收 {checks}；结构参考 {cases or '无'}"
    )


def _direction_prompt_line(direction: CreativeDirection) -> str:
    return (
        f"- {direction.identifier}: {direction.label}；{direction.description}；"
        f"重点 {' '.join(direction.guidance[:2])}"
    )
