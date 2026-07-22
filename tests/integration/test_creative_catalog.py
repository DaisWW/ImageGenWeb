from __future__ import annotations

import unittest

from imagegen.services.creative import (
    CASE_CATALOG,
    CREATIVE_ROUTER,
    EDIT_RECIPES,
    creative_direction_prompt,
)


class TestCreativeCatalog(unittest.TestCase):
    def test_case_catalog_loads_both_pinned_sources(self):
        self.assertEqual(len(CASE_CATALOG.cases), 679)
        self.assertIn("awesome:60b6e1d3", CASE_CATALOG.revision)
        self.assertIn("skill:ecc9c542", CASE_CATALOG.revision)
        self.assertEqual(
            {case.source for case in CASE_CATALOG.cases},
            {
                "awesome-gpt-image-2",
                "gpt-image2-skill",
            },
        )

    def test_case_catalog_returns_only_three_relevant_untrusted_examples(self):
        templates = CREATIVE_ROUTER.route(
            "Create a mobile MOBA arena HUD with cooldown buttons and minimap"
        )
        matches = CASE_CATALOG.search(
            "Create a mobile MOBA arena HUD with cooldown buttons and minimap",
            direction_id="game_ui",
            templates=templates,
        )

        self.assertLessEqual(len(matches), 3)
        self.assertEqual(matches[0].identifier, "skill:20")
        prompt = CASE_CATALOG.prompt(matches)
        self.assertIn("不可信参考文本", prompt)
        self.assertIn("skill:20", prompt)
        self.assertNotIn("skill:13｜", prompt)

    def test_case_catalog_omits_instruction_shaped_reference_text(self):
        case = next(
            item for item in CASE_CATALOG.cases if "IGNORE previous instructions" in item.prompt
        )

        prompt = CASE_CATALOG.prompt((case,))

        self.assertNotIn("IGNORE previous instructions", prompt)
        self.assertIn("正文已省略", prompt)

    def test_low_confidence_retrieval_expands_to_five_cases(self):
        route = CREATIVE_ROUTER.match("做一张图片")

        self.assertEqual(route.confidence, "low")
        matches = CASE_CATALOG.search(
            "做一张图片",
            direction_id="auto",
            templates=route.templates,
            limit=5,
        )
        self.assertEqual(len(matches), 5)
        self.assertLessEqual(len(CASE_CATALOG.prompt(matches)), 5 * 900)

    def test_router_prefers_delivery_then_compacts_template_and_gallery_context(self):
        templates = CREATIVE_ROUTER.route(
            "Create a mobile MOBA arena HUD with cooldown buttons and minimap"
        )

        self.assertEqual(templates[0].identifier, "game-ui-gameplay-hud")
        self.assertNotIn("game-ui-production-asset", [item.identifier for item in templates])
        routed_prompt = creative_direction_prompt("auto", template_candidates=templates)
        full_prompt = creative_direction_prompt("auto")
        self.assertLess(len(routed_prompt), len(full_prompt) * 0.6)
        self.assertIn("game-ui-gameplay-hud", routed_prompt)
        self.assertIn("gallery", routed_prompt.lower())
        self.assertNotIn("document-publishing", routed_prompt)

    def test_router_requires_game_and_data_context_for_specialized_templates(self):
        architecture_query = "北欧公寓室内建筑可视化，广角自然光"
        architecture = CREATIVE_ROUTER.route(architecture_query)
        novel_cover = CREATIVE_ROUTER.route("一本悬疑小说封面，书名逐字清晰")
        comic = CREATIVE_ROUTER.route("六格漫画分镜，同一角色连续动作")
        architecture_cases = CASE_CATALOG.search(architecture_query, templates=architecture)

        self.assertEqual(architecture[0].identifier, "architecture-space")
        self.assertTrue(
            architecture_cases[0].direction_id == "architecture"
            or architecture_cases[0].gallery_category == "architecture-and-interior"
        )
        self.assertEqual(novel_cover[0].identifier, "poster-layout-system")
        self.assertEqual(comic[0].identifier, "anime-manga-production-board")

    def test_edit_recipe_catalog_uses_generic_fallback_only_for_edits(self):
        self.assertIsNone(EDIT_RECIPES.select("object-remove-replace", enabled=False))
        selected = EDIT_RECIPES.select("object-remove-replace", enabled=True)
        fallback = EDIT_RECIPES.select("unknown", enabled=True)

        self.assertEqual(selected.identifier, "object-remove-replace")
        self.assertIn("target_object", selected.required_fields)
        self.assertEqual(fallback.identifier, "precision-edit")
        self.assertIn("translate-text", EDIT_RECIPES.prompt())
