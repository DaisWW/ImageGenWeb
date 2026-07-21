from __future__ import annotations

from .directions import CREATIVE_DIRECTIONS
from .gallery import GALLERY_ATLAS
from .matching import query_terms, text_match_score
from .models import PromptTemplate
from .templates import PROMPT_TEMPLATES

_DIRECTION_DEFAULT_TEMPLATES = {
    "game_ui": "game-ui-gameplay-hud",
    "game_art": "game-art-key-visual",
    "ui": "ui-screenshot-system",
    "infographic": "infographic-engine",
    "poster": "poster-layout-system",
    "product": "product-commerce-visual",
    "brand": "brand-identity-package",
    "architecture": "architecture-space",
    "photo": "realistic-photography",
    "illustration": "illustration-art-style",
    "character": "character-design-sheet",
    "scene": "scene-storytelling",
    "history": "history-classical-themes",
    "document": "document-publishing",
    "other": "concept-product-breakdown",
}
_PRODUCTION_ASSET_TERMS = {
    "asset",
    "component",
    "extract",
    "kit",
    "rebuild",
    "slice",
    "切图",
    "原子",
    "拆分",
    "素材",
    "组件",
}
_GAME_CONTEXT_TERMS = {
    "cooldown",
    "game",
    "gameplay",
    "gaming",
    "hud",
    "inventory",
    "minimap",
    "moba",
    "quest",
    "rpg",
    "关卡",
    "实机",
    "弹药",
    "技能",
    "游戏",
    "玩家",
    "背包",
    "血量",
}
_DATA_VISUALIZATION_TERMS = {
    "chart",
    "data",
    "dataset",
    "graph",
    "metric",
    "坐标",
    "弦图",
    "指标",
    "数据",
    "树图",
    "热图",
    "统计",
    "网络图",
    "趋势",
    "图例",
    "图表",
}


class CreativeRouter:
    def __init__(self, templates: tuple[PromptTemplate, ...]):
        self.templates = templates
        self._directions = {direction.identifier: direction for direction in CREATIVE_DIRECTIONS}

    def route(
        self,
        query: str,
        *,
        direction_id: str = "auto",
        limit: int = 3,
    ) -> tuple[PromptTemplate, ...]:
        terms = query_terms(query)
        if not terms or limit <= 0:
            return ()
        term_set = set(terms)
        locked_direction = str(direction_id or "auto").strip().lower()
        ranked: list[tuple[float, str, PromptTemplate]] = []
        for template in self.templates:
            if locked_direction not in {"", "auto"} and template.direction_id != locked_direction:
                continue
            direction = self._directions.get(template.direction_id)
            galleries = [
                category
                for identifier in GALLERY_ATLAS.for_template(template)
                if (category := GALLERY_ATLAS.get(identifier)) is not None
            ]
            score = sum(
                (
                    text_match_score(
                        terms,
                        f"{template.identifier} {template.label} {template.use_when}",
                        8,
                    ),
                    text_match_score(terms, " ".join((*template.styles, *template.scenes)), 5),
                    text_match_score(terms, " ".join(template.guidance), 2),
                    text_match_score(
                        terms,
                        " ".join(
                            f"{category.identifier} {category.label} {category.prompt_schema}"
                            for category in galleries
                        ),
                        3,
                    ),
                    text_match_score(
                        terms,
                        f"{direction.label} {direction.description}" if direction else "",
                        3,
                    ),
                )
            )
            if _DIRECTION_DEFAULT_TEMPLATES.get(template.direction_id) == template.identifier:
                score += 4.0
            if (
                locked_direction in {"", "auto"}
                and template.direction_id in {"game_ui", "game_art"}
                and not _GAME_CONTEXT_TERMS.intersection(term_set)
            ):
                score -= 20.0
            if (
                template.identifier == "game-ui-production-asset"
                and not _PRODUCTION_ASSET_TERMS.intersection(term_set)
            ):
                score -= 20.0
            if (
                template.identifier == "data-visualization-system"
                and not _DATA_VISUALIZATION_TERMS.intersection(term_set)
            ):
                score -= 20.0
            if score > 0:
                ranked.append((score, template.identifier, template))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            return ()
        minimum_score = max(8.0, ranked[0][0] * 0.35)
        return tuple(item[2] for item in ranked if item[0] >= minimum_score)[: min(limit, 3)]


CREATIVE_ROUTER = CreativeRouter(PROMPT_TEMPLATES)
