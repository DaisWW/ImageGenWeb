from __future__ import annotations

from .models import EditRecipe


class EditRecipeCatalog:
    def __init__(self, recipes: tuple[EditRecipe, ...]):
        self.recipes = recipes
        self._by_id = {recipe.identifier: recipe for recipe in recipes}

    def get(self, identifier: object) -> EditRecipe | None:
        return self._by_id.get(str(identifier or "").strip().lower())

    def select(self, identifier: object, *, enabled: bool) -> EditRecipe | None:
        if not enabled:
            return None
        return self.get(identifier) or self._by_id["precision-edit"]

    def prompt(self) -> str:
        return "\n".join(
            f"- {recipe.identifier}｜{recipe.label}｜{recipe.description}｜语法 {recipe.prompt_schema}"
            for recipe in self.recipes
        )

    @staticmethod
    def metadata(recipe: EditRecipe | None) -> dict[str, object]:
        if recipe is None:
            return {
                "edit_recipe_id": "",
                "edit_recipe_label": "",
                "edit_required_fields": [],
            }
        return {
            "edit_recipe_id": recipe.identifier,
            "edit_recipe_label": recipe.label,
            "edit_required_fields": list(recipe.required_fields),
        }


def _recipe(
    identifier: str,
    label: str,
    description: str,
    prompt_schema: str,
    required_fields: tuple[str, ...],
    hard_checks: tuple[str, ...],
) -> EditRecipe:
    return EditRecipe(
        identifier,
        label,
        description,
        prompt_schema,
        required_fields,
        hard_checks,
    )


EDIT_RECIPES = EditRecipeCatalog(
    (
        _recipe(
            "precision-edit",
            "精确局部修改",
            "通用单目标编辑，只改变用户指定内容",
            "唯一修改目标 -> 结果状态 -> 必须保持的身份/几何/布局/文字 -> 禁止连带变化",
            ("change_target", "preserve", "avoid"),
            ("用户指定的唯一修改已经完成", "未要求改变的主体、构图和内容保持不变"),
        ),
        _recipe(
            "translate-text",
            "图片文字翻译",
            "逐字替换图片文字并保持原排版",
            "原文字与目标语言 -> 逐字译文 -> 字体/字号/位置/层级不变量 -> 禁止额外文字",
            ("source_text", "target_language", "exact_text", "preserve_layout"),
            ("目标文字逐字正确且只出现一次", "原有版式、图标、Logo 和图像内容保持不变"),
        ),
        _recipe(
            "style-transfer",
            "风格迁移",
            "迁移参考图的媒介语言而不复制其主体",
            "内容图职责 -> 风格图职责 -> 需迁移的色板/笔触/材质 -> 必须保持的主体和构图",
            ("content_reference", "style_reference", "style_traits", "preserve"),
            ("主体和构图保持来自内容图", "只迁移明确指定的视觉风格特征"),
        ),
        _recipe(
            "virtual-try-on",
            "虚拟换装",
            "替换服装并保持人物身份、身体与姿态",
            "人物图 -> 服装图顺序 -> 穿着层级 -> 面料贴合与遮挡 -> 身份/身体/背景不变量",
            ("person_reference", "garment_references", "layering", "identity_preserve"),
            ("服装按参考图正确穿着并符合原姿态", "脸、肤色、体型、姿态和背景保持不变"),
        ),
        _recipe(
            "object-remove-replace",
            "物体移除 / 替换",
            "移除或替换一个明确对象并自然补全局部",
            "目标对象与位置 -> 移除或替换结果 -> 遮挡/接触/阴影 -> 相机和周围物体不变量",
            ("target_object", "target_location", "replacement", "preserve"),
            ("目标对象已按要求移除或替换", "补全区域自然且周围对象、相机和构图未漂移"),
        ),
        _recipe(
            "relight-weather",
            "光线 / 天气变换",
            "只改变时间、天气或照明条件",
            "目标环境条件 -> 光向/色温/阴影/大气 -> 地面与材质响应 -> 身份/几何/机位不变量",
            ("target_conditions", "lighting", "atmosphere", "preserve"),
            ("目标光线或天气清晰成立", "人物、物体、几何、机位和布局保持不变"),
        ),
        _recipe(
            "sketch-to-render",
            "草图转渲染",
            "将草图实现为可信成图并保持设计意图",
            "草图布局/透视 -> 目标媒介 -> 材料与光线 -> 必须保留的轮廓和部件 -> 禁止新增",
            ("sketch_reference", "target_medium", "materials", "preserve_geometry"),
            ("草图的布局、比例和透视得到保持", "渲染没有擅自增加部件、文字或设计"),
        ),
        _recipe(
            "product-cleanup",
            "商品清理",
            "清理商品背景并保护轮廓、标签和几何",
            "商品身份 -> 目标纯色背景 -> 轮廓/标签/材质不变量 -> 接触阴影 -> 禁止光晕和重设计",
            ("product_reference", "target_background", "label_preserve", "edge_quality"),
            ("商品几何、标签和材质保持准确", "轮廓干净且没有光晕、毛边或背景污染"),
        ),
        _recipe(
            "multi-image-composite",
            "多图合成",
            "按编号把多张参考图的指定元素合成到一个场景",
            "逐图编号和角色 -> 元素来源与目标位置 -> 尺度/遮挡/光线关系 -> 每张图的不变量",
            ("reference_roles", "placement", "scale", "lighting_match"),
            ("每个指定元素来自正确参考图并位于正确位置", "尺度、遮挡、光线和透视形成同一可信场景"),
        ),
        _recipe(
            "interior-swap",
            "室内换物",
            "替换室内单件家具或装饰，不重做空间",
            "待替换对象 -> 新对象参考 -> 尺寸/接触/阴影 -> 房间结构/机位/其余陈设不变量",
            ("target_object", "replacement_reference", "room_geometry", "preserve"),
            ("新对象尺寸、接触和阴影符合原空间", "房间结构、机位、光线和其他陈设保持不变"),
        ),
        _recipe(
            "character-continuity",
            "角色连续性",
            "让同一角色进入新场景并保持身份锚点",
            "角色参考 -> 新动作/场景 -> 身份锚点/服装/比例 -> 风格和色板 -> 禁止重新设计",
            ("character_reference", "new_scene", "identity_anchors", "consistency_rules"),
            ("角色身份、脸部、服装和比例与参考图一致", "只改变指定动作或场景且没有角色重设计"),
        ),
    )
)
