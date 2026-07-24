from __future__ import annotations

import re
from collections.abc import Iterable

from .matching import query_terms, text_match_score
from .models import GalleryCategory, PromptTemplate

_SKILL_REFERENCE_REVISION = "ecc9c5420c265f6677edc5f4d255bca02497ef71"
_SKILL_CASE_REF = re.compile(r"^skill:(\d+)$")


class GalleryAtlas:
    def __init__(
        self,
        categories: tuple[GalleryCategory, ...],
        direction_defaults: dict[str, tuple[str, ...]],
        *,
        revision: str,
    ):
        self.categories = categories
        self.revision = revision
        self.base_url = (
            "https://github.com/wuyoscar/GPT-Image2-Skill/blob/"
            f"{revision}/skills/gpt-image/references"
        )
        self.index_url = f"{self.base_url}/gallery.md"
        self._by_id = {category.identifier: category for category in categories}
        self._by_case_id = {
            case_id: category
            for category in categories
            for case_id in range(category.case_start, category.case_end + 1)
        }
        self._direction_defaults = direction_defaults

    def get(self, identifier: object) -> GalleryCategory | None:
        return self._by_id.get(str(identifier or "").strip().lower())

    def match(
        self,
        query: str,
        *,
        direction_id: str | None = None,
        limit: int = 3,
    ) -> tuple[str, ...]:
        terms = query_terms(query)
        if not terms or limit <= 0:
            return ()
        normalized_query = str(query or "").strip().lower()
        ranked: list[tuple[float, str]] = []
        for category in self.categories:
            if not self._compatible(category, direction_id):
                continue
            identifier_text = category.identifier.replace("-", " ")
            score = text_match_score(
                terms,
                f"{identifier_text} {category.label} {category.prompt_schema}",
                4,
            )
            if category.identifier in normalized_query or identifier_text in normalized_query:
                score += 40.0
            if category.label.lower() in normalized_query:
                score += 40.0
            if score > 0:
                ranked.append((score, category.identifier))
        if not ranked:
            return ()
        ranked.sort(key=lambda item: (-item[0], item[1]))
        minimum_score = max(8.0, ranked[0][0] * 0.35)
        return tuple(identifier for score, identifier in ranked if score >= minimum_score)[
            : min(limit, 3)
        ]

    def compatible(self, identifier: object, direction_id: str | None) -> bool:
        category = self.get(identifier)
        return category is not None and self._compatible(category, direction_id)

    def source_url(self, identifier: str) -> str:
        category = self._by_id[identifier]
        return f"{self.base_url}/{category.source_file}"

    def select(
        self,
        value: object,
        *,
        preferred: Iterable[str] = (),
        case_refs: Iterable[str] = (),
        direction_id: str | None = None,
    ) -> list[str]:
        selected = self._normalize(value, direction_id=direction_id)
        if selected:
            return selected
        selected = self._normalize(tuple(preferred), direction_id=direction_id)
        if selected:
            return selected
        selected = self._from_case_refs(case_refs, direction_id=direction_id)
        if selected:
            return selected
        return self._normalize(
            self._direction_defaults.get(str(direction_id or "").lower(), ()),
            direction_id=direction_id,
        )

    def for_template(self, template: PromptTemplate) -> tuple[str, ...]:
        case_refs = list(template.case_refs) or [
            f"awesome:{case_id}" for case_id in template.example_case_ids
        ]
        return tuple(
            self.select(
                None,
                preferred=template.gallery_categories,
                case_refs=case_refs,
                direction_id=template.direction_id,
            )
        )

    def prompt(
        self,
        direction_id: str | None = None,
        *,
        identifiers: Iterable[str] | None = None,
    ) -> str:
        selected = set(identifiers) if identifiers is not None else None
        return "\n".join(
            f"- {category.identifier}｜{category.label}｜"
            f"Case {category.case_start}-{category.case_end}｜"
            f"方向 {','.join(category.direction_ids)}｜语法 {category.prompt_schema}"
            for category in self.categories
            if self._compatible(category, direction_id)
            and (selected is None or category.identifier in selected)
        )

    def metadata(self, identifiers: Iterable[str]) -> dict[str, list[str]]:
        categories = [
            category for identifier in identifiers if (category := self.get(identifier)) is not None
        ]
        return {
            "gallery_categories": [category.identifier for category in categories],
            "gallery_category_labels": [category.label for category in categories],
            "gallery_case_ranges": [category.case_ref for category in categories],
            "gallery_category_urls": [
                self.source_url(category.identifier) for category in categories
            ],
        }

    def _normalize(self, value: object, *, direction_id: str | None) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        result: list[str] = []
        for item in value:
            category = self.get(item)
            if category is not None and self._compatible(category, direction_id):
                if category.identifier not in result:
                    result.append(category.identifier)
            if len(result) >= 3:
                break
        return result

    def _from_case_refs(
        self,
        case_refs: Iterable[str],
        *,
        direction_id: str | None,
    ) -> list[str]:
        category_ids = []
        for case_ref in case_refs:
            match = _SKILL_CASE_REF.fullmatch(str(case_ref).strip().lower())
            category = self._by_case_id.get(int(match.group(1))) if match else None
            if category is not None:
                category_ids.append(category.identifier)
        return self._normalize(category_ids, direction_id=direction_id)

    @staticmethod
    def _compatible(category: GalleryCategory, direction_id: str | None) -> bool:
        return (
            direction_id is None
            or category.identifier == "edit-endpoint-showcase"
            or direction_id in category.direction_ids
        )


def _category(
    identifier: str,
    label: str,
    case_start: int,
    case_end: int,
    direction_ids: tuple[str, ...],
    prompt_schema: str,
) -> GalleryCategory:
    return GalleryCategory(
        identifier,
        label,
        case_start,
        case_end,
        direction_ids,
        prompt_schema,
    )


_GALLERY_CATEGORIES = (
    _category(
        "anime-and-manga",
        "动漫与漫画",
        1,
        12,
        ("illustration", "character", "scene", "game_art"),
        "原创角色与身份锚点 -> 动作或分镜职责 -> 环境 -> 线稿/赛璐璐/网点语言 -> 跨格一致性",
    ),
    _category(
        "gaming",
        "游戏视觉",
        13,
        22,
        ("game_ui", "game_art"),
        "游戏平台与镜头 -> 可玩场景 -> HUD/制作交付物 -> 真实状态与数字 -> 原创 IP 和一致性",
    ),
    _category(
        "retro-and-cyberpunk",
        "复古与赛博朋克",
        23,
        25,
        ("illustration", "scene", "game_art"),
        "时代或网格格式 -> 原创人物/物件 -> 霓虹与工业材质 -> 受控色板 -> 避免品牌和假文字",
    ),
    _category(
        "cinematic-and-animation",
        "电影与动画",
        26,
        30,
        ("scene", "character", "illustration", "game_art"),
        "镜头数量与顺序 -> 每镜叙事节拍 -> 景别/机位/运动 -> 角色身份 -> 连续光线和轴线",
    ),
    _category(
        "character-design",
        "角色设计",
        31,
        32,
        ("character", "game_art"),
        "角色身份锚点 -> 正侧背视图 -> 表情/部件 -> 服装材质与色板 -> 非对称特征方向",
    ),
    _category(
        "typography-and-posters",
        "排版与海报",
        33,
        45,
        ("poster",),
        "画幅与固定版区 -> 主视觉 -> 引号内精确文字 -> 三级促销层级 -> 远距离可读与禁用假文案",
    ),
    _category(
        "illustration",
        "插画",
        46,
        47,
        ("illustration",),
        "画幅与主体 -> 具体媒介 -> 笔触和边缘 -> 纸张/画布质感 -> 有限色板与完成度",
    ),
    _category(
        "watercolor",
        "水彩",
        48,
        49,
        ("illustration",),
        "主体与留白 -> 透明叠色 -> 湿画/干画边缘 -> 纸张吸水纹理 -> 避免塑料数码质感",
    ),
    _category(
        "ink-and-chinese",
        "水墨与新中式",
        50,
        51,
        ("illustration", "history", "poster"),
        "地域与文化语境 -> 工笔结构/写意墨韵 -> 宣纸留白 -> 有限设色 -> 精确中文与时代一致性",
    ),
    _category(
        "pixel-art",
        "像素艺术",
        52,
        53,
        ("illustration", "game_art", "game_ui"),
        "目标分辨率与视角 -> 像素网格 -> 有限色板 -> 清晰轮廓与光源 -> 禁止抗锯齿和混合像素密度",
    ),
    _category(
        "isometric",
        "等距视角",
        54,
        55,
        ("illustration", "architecture", "game_art"),
        "等距轴线与画布 -> 空间模块 -> 层级和动线 -> 材质与阴影 -> 统一比例、禁止透视漂移",
    ),
    _category(
        "product-and-food",
        "商品与食品",
        56,
        59,
        ("product", "poster"),
        "商品/食品几何 -> 环境 -> 材质 -> 独立光线系统 -> 动态细节 -> JSON/config 分区与商业层级",
    ),
    _category(
        "brand-systems-and-identity",
        "品牌系统与识别",
        60,
        62,
        ("brand",),
        "原创标志/字标 -> 色彩与字体系统 -> 包装/社媒/数字触点 -> 网格展示 -> 跨触点一致性",
    ),
    _category(
        "photography",
        "摄影",
        63,
        66,
        ("photo",),
        "单一拍摄设备与视角 -> 时间地点 -> 5-12 个现实物件 -> 自然瑕疵 -> 光线和可信捕捉语境",
    ),
    _category(
        "infographics-and-field-guides",
        "信息图与图鉴",
        67,
        74,
        ("infographic", "document"),
        "产物类型 -> 固定版区 -> 编号标注与引线 -> 图例/单位 -> 博物馆、图鉴或教育风格边界",
    ),
    _category(
        "research-paper-figures",
        "科研论文图",
        75,
        95,
        ("infographic", "document"),
        "论文画幅与面板 -> 节点/列/栈/图表语法 -> 有向关系 -> 精确标签和图例 -> 出版级白底可读性",
    ),
    _category(
        "official-openai-cookbook-examples",
        "OpenAI 官方示例",
        96,
        99,
        ("other",),
        "只复用官方能力和参数语义；按当前交付物重写主体、文字和约束，不把示例身份当作风格",
    ),
    _category(
        "edit-endpoint-showcase",
        "参考图编辑",
        100,
        101,
        ("photo", "product", "poster", "other"),
        "先写唯一修改目标 -> 按参考图编号分配职责 -> 重复身份/构图/文字不变量 -> 只改变 X",
    ),
    _category(
        "ui-ux-mockups",
        "UI/UX 样机",
        102,
        106,
        ("ui",),
        "产品与设备画布 -> 信息架构 -> 组件和状态 -> 真实数据与精确文案 -> 对齐、间距和生产级可读性",
    ),
    _category(
        "data-visualization",
        "数据可视化",
        107,
        111,
        ("infographic",),
        "图表家族 -> 数据结构 -> 轴/单位/图例 -> 颜色、尺寸或连线编码 -> 重复面板统一尺度",
    ),
    _category(
        "technical-illustration",
        "技术插图",
        112,
        116,
        ("infographic", "other", "game_art"),
        "爆炸/剖切视图 -> 有序部件 -> 编号引线 -> 功能、材料和尺度 -> 工程图或技术图版边界",
    ),
    _category(
        "architecture-and-interior",
        "建筑与室内",
        117,
        121,
        ("architecture",),
        "空间功能 -> 单一机位与镜头感 -> 真实尺度 -> 具体材料 -> 主光方向、阴影和负空间",
    ),
    _category(
        "scientific-and-educational",
        "科学与教育",
        122,
        128,
        ("infographic", "document"),
        "准确主题与视图 -> 教学固定版区 -> 标注、图例和尺度 -> 克制学术色板 -> 禁止误导性细节",
    ),
    _category(
        "fashion-editorial",
        "时尚编辑",
        129,
        135,
        ("photo", "character", "poster"),
        "明确成年主体 -> 造型和面料 -> 姿态 -> 场景与单一镜头 -> 杂志层级、克制文字和非露骨边界",
    ),
    _category(
        "fine-art-painting",
        "纯艺术绘画",
        136,
        140,
        ("illustration",),
        "艺术运动或媒介特征 -> 构图 -> 笔触/颜料层 -> 画布表面 -> 原创主题而非复制艺术家作品",
    ),
    _category(
        "more-illustration-styles",
        "扩展插画风格",
        141,
        146,
        ("illustration", "character"),
        "具体媒介与生产语境 -> 主体 -> 形状和线条规则 -> 材质与色板 -> 风格边界和原创性",
    ),
    _category(
        "cinematic-film-references",
        "电影镜头参考",
        147,
        152,
        ("scene", "photo", "game_art"),
        "叙事瞬间 -> 景别/机位/焦段感 -> 调度与前中后景 -> 主光和色温 -> 胶片或数字捕捉特征",
    ),
    _category(
        "beauty-and-lifestyle",
        "美妆与生活方式",
        153,
        154,
        ("photo", "product"),
        "成年主体或产品 -> 自然姿态 -> 皮肤/材质细节 -> 柔和真实光线 -> 品牌控制与生活化环境",
    ),
    _category(
        "events-and-experience",
        "活动与体验",
        155,
        156,
        ("poster", "scene"),
        "活动类型与受众 -> 主视觉 -> 日期地点等精确文案 -> 体验场景 -> CTA、远读层级和假赞助商禁令",
    ),
    _category(
        "tattoo-design",
        "纹身设计",
        157,
        160,
        ("illustration", "character"),
        "身体部位与可纹性 -> 流派 -> 线条/灰度/色彩 -> 负空间 -> 平面 flash 展示且不生成真人皮肤",
    ),
    _category(
        "screen-photography",
        "屏幕实拍",
        161,
        162,
        ("photo", "ui", "game_ui"),
        "屏幕内容 -> 拍摄设备与距离 -> 摩尔纹/反射/轻微模糊 -> 周边环境 -> 保持屏幕布局可辨认",
    ),
)

_DIRECTION_CATEGORY_DEFAULTS = {
    "game_ui": ("gaming",),
    "game_art": ("gaming",),
    "ui": ("ui-ux-mockups",),
    "infographic": ("infographics-and-field-guides",),
    "poster": ("typography-and-posters",),
    "product": ("product-and-food",),
    "brand": ("brand-systems-and-identity",),
    "architecture": ("architecture-and-interior",),
    "photo": ("photography",),
    "illustration": ("illustration",),
    "character": ("character-design",),
    "scene": ("cinematic-and-animation",),
    "history": ("ink-and-chinese",),
    "document": ("infographics-and-field-guides",),
}

GALLERY_ATLAS = GalleryAtlas(
    _GALLERY_CATEGORIES,
    _DIRECTION_CATEGORY_DEFAULTS,
    revision=_SKILL_REFERENCE_REVISION,
)
