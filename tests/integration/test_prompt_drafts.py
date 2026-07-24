from __future__ import annotations

import json

from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import (
    ConversationMessage,
    RuntimeLog,
)
from imagegen.services import ServiceError
from imagegen.services.creative import (
    CREATIVE_DIRECTIONS,
    GALLERY_ATLAS,
    PROMPT_TEMPLATES,
    SCENE_TAG_LABELS,
    STYLE_TAG_LABELS,
    creative_direction_dicts,
    gallery_category_dicts,
)
from tests.support.platform import (
    PlatformTestCase,
    png_bytes,
)


class TestPromptDrafts(PlatformTestCase):
    def test_creative_catalog_keeps_all_source_dimensions(self):
        self.assertEqual(len(CREATIVE_DIRECTIONS), 15)
        self.assertEqual(len(STYLE_TAG_LABELS), 47)
        self.assertEqual(len(SCENE_TAG_LABELS), 15)
        self.assertEqual(len(PROMPT_TEMPLATES), 42)
        self.assertEqual(len(GALLERY_ATLAS.categories), 31)
        gallery_options = gallery_category_dicts()
        self.assertEqual(len(gallery_options), 32)
        self.assertEqual(gallery_options[0]["id"], "auto")
        self.assertEqual(
            {item["id"] for item in gallery_options[1:]},
            {category.identifier for category in GALLERY_ATLAS.categories},
        )
        self.assertEqual(
            len({category.identifier for category in GALLERY_ATLAS.categories}),
            len(GALLERY_ATLAS.categories),
        )
        expected_case = 1
        direction_ids = {direction.identifier for direction in CREATIVE_DIRECTIONS}
        for category in GALLERY_ATLAS.categories:
            self.assertEqual(category.case_start, expected_case)
            self.assertGreaterEqual(category.case_end, category.case_start)
            self.assertTrue(set(category.direction_ids) <= direction_ids)
            expected_case = category.case_end + 1
        self.assertEqual(expected_case, 163)
        for template in PROMPT_TEMPLATES:
            self.assertTrue(set(template.styles) <= set(STYLE_TAG_LABELS))
            self.assertTrue(set(template.scenes) <= set(SCENE_TAG_LABELS))
            gallery_ids = GALLERY_ATLAS.select(
                None,
                preferred=template.gallery_categories,
                case_refs=template.case_refs,
                direction_id=template.direction_id,
            )
            self.assertTrue(gallery_ids, template.identifier)
            self.assertTrue(set(template.gallery_categories) <= set(gallery_ids))
            self.assertTrue(all(GALLERY_ATLAS.get(identifier) for identifier in gallery_ids))
        metadata = GALLERY_ATLAS.metadata(["research-paper-figures"])
        self.assertEqual(metadata["gallery_case_ranges"], ["skill:75-95"])
        self.assertIn(GALLERY_ATLAS.revision, metadata["gallery_category_urls"][0])
        for category in GALLERY_ATLAS.categories:
            self.assertEqual(
                GALLERY_ATLAS.match(
                    f"{category.identifier} {category.label}",
                    direction_id=category.direction_ids[0],
                )[0],
                category.identifier,
            )

    def test_gallery_atlas_uses_explicit_template_case_and_direction_fallbacks(self):
        self.assertEqual(
            GALLERY_ATLAS.select(
                ["watercolor"],
                preferred=("illustration",),
                case_refs=("skill:46",),
                direction_id="illustration",
            ),
            ["watercolor"],
        )
        self.assertEqual(
            GALLERY_ATLAS.select(
                None,
                preferred=("tattoo-design",),
                case_refs=("skill:46",),
                direction_id="illustration",
            ),
            ["tattoo-design"],
        )
        self.assertEqual(
            GALLERY_ATLAS.select(
                None,
                case_refs=("skill:107",),
                direction_id="infographic",
            ),
            ["data-visualization"],
        )
        self.assertEqual(
            GALLERY_ATLAS.select(None, direction_id="brand"),
            ["brand-systems-and-identity"],
        )
        self.assertEqual(
            GALLERY_ATLAS.select(
                ["edit-endpoint-showcase"],
                direction_id="character",
            ),
            ["edit-endpoint-showcase"],
        )

    def test_game_directions_are_first_concrete_choices_and_have_production_contracts(self):
        directions = creative_direction_dicts()
        self.assertEqual([item["id"] for item in directions[:3]], ["auto", "game_ui", "game_art"])
        game_templates = [
            template
            for template in PROMPT_TEMPLATES
            if template.direction_id in {"game_ui", "game_art"}
        ]
        production_asset = next(
            template
            for template in game_templates
            if template.identifier == "game-ui-production-asset"
        )
        self.assertIn("atomic_asset", production_asset.required_fields)
        self.assertIn("最终图片只包含一个选定的原子 UI 资源", production_asset.hard_checks)
        self.assertEqual(len(game_templates), 11)
        self.assertTrue(
            all(
                template.case_refs
                for template in game_templates
                if template.identifier != "game-ui-production-asset"
            )
        )
        self.assertTrue(all(template.required_fields for template in game_templates))
        self.assertTrue(all(template.hard_checks for template in game_templates))
        character_sheet = next(
            template
            for template in game_templates
            if template.identifier == "game-art-character-sheet"
        )
        self.assertIn("directional_identity_map", character_sheet.required_fields)
        gameplay_hud = next(
            template for template in game_templates if template.identifier == "game-ui-gameplay-hud"
        )
        self.assertTrue(any("全幅装饰框" in pitfall for pitfall in gameplay_hud.pitfalls))

    def test_prompt_draft_auto_selects_catalog_template_and_preserves_locked_direction(self):
        workspace = self.create_workspace("自动匹配海报")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="做一张运动鞋新品发布海报，标题是 AIR ZERO。",
        )
        chat_system = self.chat_client.calls[-1]["system"]
        self.assertIn("本次调用同时完成需求确认和最终提示词整理", chat_system)
        self.assertIn("poster-layout-system", chat_system)
        self.assertIn("sports-campaign-poster", chat_system)
        self.assertNotIn("ui-screenshot-system", chat_system)
        self.assertIn("第三方案例", chat_system)
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "运动鞋新品发布海报，标题 AIR ZERO。",
                "prompt": '3:4 vertical poster with exact title "AIR ZERO".',
                "creative_direction": "poster",
                "template_id": "poster-layout-system",
                "gallery_categories": ["typography-and-posters"],
                "style_tags": ["Poster"],
                "scene_tags": ["Commerce", "Social"],
                "selection_reason": "交付物是商业发布海报，需要明确主视觉与标题层级。",
                "brief": {
                    "deliverable": "新品发布海报",
                    "subject": "运动鞋",
                    "exact_text": ["AIR ZERO"],
                },
                "hard_checks": ["标题必须逐字显示 AIR ZERO", "只出现一双主运动鞋"],
                "quality_hint": "high",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=True,
            creative_direction_id="auto",
        )

        self.assertEqual(draft.payload["creative_direction"], "poster")
        self.assertEqual(draft.payload["template_id"], "poster-layout-system")
        self.assertEqual(draft.payload["template_label"], "海报排版系统")
        self.assertEqual(draft.payload["gallery_categories"], ["typography-and-posters"])
        self.assertEqual(draft.payload["gallery_category_labels"], ["排版与海报"])
        self.assertEqual(draft.payload["gallery_case_ranges"], ["skill:33-45"])
        self.assertEqual(draft.payload["style_labels"], ["海报"])
        self.assertEqual(draft.payload["scene_labels"], ["商业", "社媒"])
        self.assertIn("主视觉与标题层级", draft.payload["selection_reason"])
        self.assertEqual(draft.payload["sources"][1]["url"], "https://gpt-image2.canghe.ai/")
        system = self.chat_client.calls[-1]["system"]
        self.assertIn("交付物分类 → Gallery Atlas 类别 → 视觉风格", system)
        self.assertIn("poster-layout-system", system)
        self.assertNotIn("concept-product-breakdown", system)
        self.assertIn("Gallery Atlas 路由", system)
        self.assertIn("typography-and-posters", system)
        self.assertIn("若用户从外部图库复制提示词", system)
        self.assertIn("Production contract", draft.payload["prompt"])
        self.assertIn('"verbatim": "AIR ZERO"', draft.payload["prompt"])
        self.assertTrue(any("AIR ZERO" in item for item in draft.payload["hard_checks"]))

        locked = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            creative_direction_id="product",
        )
        self.assertEqual(locked.payload["creative_direction"], "product")
        self.assertEqual(locked.payload["template_id"], "custom")
        self.assertEqual(locked.payload["gallery_categories"], ["product-and-food"])

    def test_prompt_draft_preserves_locked_gallery_category_before_template(self):
        workspace = self.create_workspace("锁定水彩图谱")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="画一幅雨后花园水彩插画，保留纸张纹理和透明叠色。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "雨后花园水彩插画。",
                "prompt": "雨后花园，透明水彩叠色与可见纸张纹理。",
                "creative_direction": "illustration",
                "template_id": "anime-manga-production-board",
                "gallery_categories": ["typography-and-posters"],
                "style_tags": ["Watercolor"],
                "scene_tags": ["Editorial"],
                "selection_reason": "用户锁定了水彩图谱。",
                "brief": {"deliverable": "水彩插画", "subject": "雨后花园"},
                "hard_checks": ["保留透明叠色和纸张纹理"],
                "quality_hint": "medium",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            creative_direction_id="auto",
            gallery_category_id="watercolor",
        )

        self.assertEqual(draft.payload["creative_direction"], "illustration")
        self.assertEqual(draft.payload["template_id"], "custom")
        self.assertEqual(draft.payload["gallery_categories"], ["watercolor"])
        self.assertEqual(draft.payload["gallery_category_labels"], ["水彩"])
        self.assertEqual(workspace.settings["gallery_category_id"], "watercolor")
        self.assertIn(
            "用户已锁定 Gallery 类别 `watercolor`",
            self.chat_client.calls[-1]["system"],
        )

    def test_game_ui_draft_keeps_template_case_refs_and_production_spec(self):
        workspace = self.create_workspace("游戏 HUD")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="原创科幻动作游戏的移动端战斗 HUD，显示护盾、弹药和任务目标。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "移动端原创科幻动作游戏战斗 HUD。",
                "prompt": "single mobile gameplay HUD with shield, ammo and quest objective",
                "creative_direction": "game_ui",
                "template_id": "game-ui-gameplay-hud",
                "style_tags": ["Game UI", "HUD"],
                "scene_tags": ["Gaming"],
                "selection_reason": "交付物是单一实机 HUD，需要固定平台和分区。",
                "production_spec": {
                    "platform": "mobile",
                    "canvas": "16:9 gameplay screen",
                    "screen_type": "gameplay_hud",
                    "safe_area": "内缩 5%",
                    "hud_zones": ["左上护盾", "右上弹药", "左下任务"],
                    "identity_anchors": ["原创科幻游戏视觉语言"],
                    "exact_text": ["OBJECTIVE: REACH THE GATE"],
                    "ui_constraints": ["不遮挡角色和敌人"],
                },
                "hard_checks": [
                    "HUD 分区不越界",
                    "关键数值可读",
                    "平台正确",
                    "画布正确",
                    "不遮挡主体",
                    "无额外文字",
                ],
                "quality_hint": "high",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=True,
            creative_direction_id="game_ui",
        )

        self.assertEqual(draft.payload["creative_direction"], "game_ui")
        self.assertEqual(draft.payload["template_id"], "game-ui-gameplay-hud")
        self.assertEqual(draft.payload["case_refs"], ["skill:15", "skill:18", "skill:19"])
        self.assertEqual(draft.payload["production_spec"]["platform"], "mobile")
        self.assertEqual(draft.payload["prompt"].count('"exact_text"'), 1)
        self.assertEqual(len(draft.payload["hard_checks"]), 6)
        self.assertIn("gallery-gaming.md", draft.payload["sources"][2]["references"]["gaming"])
        self.assertIn("game-ui-gameplay-hud", self.chat_client.calls[-1]["system"])
        self.assertIn("平台、目标画布、屏幕状态和安全区", self.chat_client.calls[-1]["system"])

    def test_game_ui_production_asset_keeps_reconstruction_contract(self):
        workspace = self.create_workspace("游戏 UI Kit")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="参考完整 HUD，只重建护盾图标，不要抠图、文字或图集。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "参考 HUD 风格重建一个独立护盾图标。",
                "prompt": "one isolated shield icon on a genuinely transparent canvas",
                "reference_usage": "generation",
                "reference_reason": "参考图用于保持 HUD 的轮廓、材质与配色。",
                "creative_direction": "game_ui",
                "template_id": "game-ui-production-asset",
                "style_tags": ["Game UI", "HUD"],
                "scene_tags": ["Gaming", "Tech"],
                "selection_reason": "目标是一个可直接用于引擎的原子 UI 图标。",
                "production_spec": {
                    "platform": "PC",
                    "component_tree": ["状态模块 → 护盾图标", "状态模块 → 状态条"],
                    "asset_module": "状态模块",
                    "atomic_asset": "护盾图标",
                    "asset_type": "icon",
                    "runtime_content": ["护盾文字", "42/100 动态数值"],
                    "transparent_output": "PNG alpha",
                    "nine_slice": "不适用",
                    "reconstruction_rules": ["只参考风格，不复制背景像素"],
                },
                "hard_checks": ["只有一个护盾图标"],
                "quality_hint": "medium",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            creative_direction_id="game_ui",
        )

        self.assertEqual(draft.payload["template_id"], "game-ui-production-asset")
        self.assertEqual(draft.payload["production_spec"]["atomic_asset"], "护盾图标")
        self.assertEqual(
            draft.payload["production_spec"]["runtime_content"],
            ["护盾文字", "42/100 动态数值"],
        )
        self.assertIn("最终图片只包含一个选定的原子 UI 资源", draft.payload["hard_checks"])
        self.assertIn("禁止抠图", self.chat_client.calls[-1]["system"])

    def test_game_ui_component_tree_clarification_keeps_selection_question(self):
        workspace = self.create_workspace("游戏 UI Kit 组件树")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="先拆解完整 HUD，再让我选择一个原子资源。",
        )
        component_tree = "组件树：\n" + "\n".join(
            f"• 模块 {index} → 空面板、边框、图标、装饰、状态条轨道与填充" for index in range(1, 17)
        )
        selection = "请选择本次重建的一个原子资源，例如：玩家状态模块 → 横向状态空面板。"
        question = f"{component_tree}\n\n{selection}"
        self.assertGreater(len(question), 500)
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": [question],
                "creative_direction": "game_ui",
            },
            ensure_ascii=False,
        )

        clarification = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            creative_direction_id="game_ui",
        )

        self.assertEqual(clarification.payload["questions"], [question])
        self.assertIn(selection, clarification.content)

    def test_prompt_draft_injects_only_retrieved_case_matches(self):
        workspace = self.create_workspace("MOBA HUD 案例检索")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="Create a mobile MOBA arena HUD with cooldown buttons and minimap.",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "移动 MOBA 实机 HUD。",
                "prompt": "original mobile MOBA gameplay HUD",
                "creative_direction": "game_ui",
                "template_id": "game-ui-gameplay-hud",
                "gallery_categories": ["gaming"],
                "style_tags": ["Game UI", "HUD"],
                "scene_tags": ["Gaming"],
                "selection_reason": "交付物是移动 MOBA 实机 HUD。",
                "brief": {"deliverable": "移动 MOBA HUD"},
                "hard_checks": ["HUD 包含小地图和技能冷却按钮"],
                "quality_hint": "medium",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            creative_direction_id="game_ui",
        )

        self.assertLessEqual(len(draft.payload["retrieved_cases"]), 3)
        self.assertEqual(draft.payload["retrieved_cases"][0]["id"], "skill:20")
        system = self.chat_client.calls[-1]["system"]
        self.assertIn("第三方案例", system)
        self.assertIn("skill:20", system)
        self.assertNotIn("skill:13｜", system)

    def test_img2img_prompt_draft_normalizes_edit_recipe(self):
        workspace = self.create_workspace("商品移除对象")
        asset = self.services.workspaces.add_assets(
            workspace,
            [("product.png", png_bytes())],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="只移除商品右侧的花瓶，其他都保持不变。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "移除商品右侧花瓶。",
                "prompt": "只移除右侧花瓶；保持商品、背景、机位和光线不变。",
                "reference_usage": "generation",
                "reference_reason": "原图是待编辑内容。",
                "creative_direction": "product",
                "template_id": "product-commerce-visual",
                "edit_recipe_id": "object-remove-replace",
                "gallery_categories": ["edit-endpoint-showcase"],
                "style_tags": ["Product"],
                "scene_tags": ["Commerce"],
                "selection_reason": "单对象局部移除。",
                "brief": {
                    "deliverable": "编辑后的商品图",
                    "reference_plan": [
                        {
                            "image_number": 1,
                            "role": "待编辑原图",
                            "preserve": ["商品", "背景", "机位", "光线"],
                            "change": ["移除右侧花瓶"],
                        }
                    ],
                    "change": ["移除右侧花瓶"],
                    "preserve": ["商品", "背景", "机位", "光线"],
                },
                "hard_checks": ["右侧花瓶已经移除"],
                "quality_hint": "medium",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
            reference_ids=(asset.id,),
            creative_direction_id="product",
        )

        self.assertEqual(draft.payload["edit_recipe_id"], "object-remove-replace")
        self.assertEqual(draft.payload["edit_recipe_label"], "物体移除 / 替换")
        self.assertIn("target_object", draft.payload["edit_required_fields"])
        self.assertIn("目标对象已按要求移除或替换", draft.payload["hard_checks"])
        self.assertIn("object-remove-replace", self.chat_client.calls[-1]["system"])
        self.assertIn('"change_only"', draft.payload["prompt"])
        self.assertIn("移除右侧花瓶", draft.payload["prompt"])
        self.assertIn('"must_preserve"', draft.payload["prompt"])
        self.assertIn('"reference_roles"', draft.payload["prompt"])
        self.assertEqual(draft.payload["brief"]["reference_plan"][0]["role"], "待编辑原图")

    def test_prompt_draft_normalizes_explicit_canvas_request(self):
        workspace = self.create_workspace("画幅结构化")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="做一张 1920×1080 的 16:9 横屏画面。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "1920×1080 横屏画面。",
                "prompt": "wide 16:9 scene",
                "canvas_request": {"width": "1920", "height": 1080, "aspect_ratio": "16:9"},
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )

        self.assertEqual(
            draft.payload["canvas_request"],
            {"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
        )
        self.assertIn("canvas_request", self.chat_client.calls[-1]["system"])

    def test_prompt_draft_rejects_invalid_structured_contract_and_records_it(self):
        workspace = self.create_workspace("无效结构化输出")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="生成一张运动鞋海报",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": None,
                "prompt": ["private-invalid-prompt"],
            }
        )

        with self.assertRaises(ServiceError) as raised:
            self.services.conversations.create_prompt_draft(
                workspace,
                model_id="test-chat",
                translate_to_english=False,
            )

        self.assertEqual(raised.exception.code, "chat_invalid_response")
        self.assertEqual(raised.exception.status_code, 502)
        entry = db.session.get(RuntimeLog, raised.exception.error_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.event, "chat.prompt_draft")
        self.assertEqual(entry.error_code, "chat_invalid_response")
        self.assertEqual(
            entry.details["diagnostics"]["validation"],
            "structured_output_contract",
        )
        self.assertNotIn("private-invalid-prompt", json.dumps(entry.details))

    def test_prompt_draft_does_not_stringify_invalid_optional_text(self):
        workspace = self.create_workspace("可选字段类型")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="生成一张运动鞋海报",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "运动鞋海报",
                "prompt": "centered product poster",
                "reference_reason": None,
                "selection_reason": {"invalid": "value"},
                "brief": {
                    "deliverable": None,
                    "subject": ["shoe"],
                    "composition": " centered ",
                    "reference_plan": [{"image_number": 1, "role": None}],
                },
            }
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )

        self.assertEqual(draft.payload["reference_reason"], "")
        self.assertEqual(draft.payload["selection_reason"], "")
        self.assertEqual(draft.payload["brief"]["deliverable"], "")
        self.assertEqual(draft.payload["brief"]["subject"], "")
        self.assertEqual(draft.payload["brief"]["composition"], "centered")
        self.assertEqual(draft.payload["brief"]["reference_plan"], [])

    def test_game_art_draft_preserves_directional_identity_map(self):
        workspace = self.create_workspace("角色设定表")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="六面板女性剑士设定表，角色右侧白发、左肩白甲、右臂机械护具。",
        )
        directional_map = [
            "FRONT：角色右侧 → 观看者左侧 → 白发与右臂护具",
            "BACK：角色左侧 → 观看者左侧 → 白色肩甲",
            "FACE：角色右侧 → 观看者左侧 → 白发",
        ]
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "六面板女性剑士正式角色设定表。",
                "prompt": "six-panel production character sheet",
                "creative_direction": "game_art",
                "template_id": "game-art-character-sheet",
                "production_spec": {
                    "panel_count": 6,
                    "directional_identity_map": directional_map,
                },
                "hard_checks": ["六面板数量正确"],
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=True,
            creative_direction_id="game_art",
        )

        self.assertEqual(draft.payload["template_id"], "game-art-character-sheet")
        self.assertEqual(
            draft.payload["production_spec"]["directional_identity_map"], directional_map
        )
        self.assertLessEqual(len(draft.payload["hard_checks"]), 6)

    def test_prompt_translation_defaults_off_and_records_draft_duration(self):
        workspace = self.create_workspace()
        self.assertFalse(workspace.settings["translate_prompt"])
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="电影感人物肖像",
        )
        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )
        self.assertEqual(draft.payload["language"], "zh")
        self.assertEqual(float(draft.elapsed_seconds), 1.234)
        self.assertEqual(draft.provider_id, "test-chat")
        self.assertEqual(self.chat_client.calls[-1]["model_id"], "test-chat")
        self.assertEqual(self.chat_client.calls[-1]["reasoning_effort"], "medium")
        self.assertIn("中文生图提示词", self.chat_client.calls[-1]["system"])

        translated = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=True,
        )
        self.assertEqual(translated.payload["language"], "en")
        self.assertIn("英文生图提示词", self.chat_client.calls[-1]["system"])

    def test_prompt_draft_requires_clarification_before_creating_final_prompt(self):
        workspace = self.create_workspace("模糊海报")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="帮我做一张好看的海报",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": [
                    "海报用于什么场景？\nA. 活动宣传（推荐）\nB. 产品推广\nC. 社交媒体\nD. 其他（请自定义）",
                    "主视觉主体是什么？\nA. 人物\nB. 产品（推荐）\nC. 抽象图形\nD. 其他（请自定义）",
                    "画幅比例是什么？\nA. 竖版 3:4（推荐）\nB. 横版 16:9\nC. 方形 1:1\nD. 其他（请自定义）",
                    "画面是否包含文字？\nA. 不含文字（推荐）\nB. 仅标题\nC. 标题和副文案\nD. 其他（请自定义）",
                    "背景环境是什么？\nA. 纯色背景（推荐）\nB. 室内场景\nC. 户外场景\nD. 其他（请自定义）",
                ],
            },
            ensure_ascii=False,
        )

        clarification = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )

        self.assertEqual(clarification.kind, "message")
        self.assertEqual(clarification.payload["status"], "needs_clarification")
        self.assertEqual(len(clarification.payload["questions"]), 4)
        self.assertNotIn("prompt", clarification.payload)
        self.assertIn("还需要确认", clarification.content)
        self.assertIn("4. 画面是否包含文字？", clarification.content)
        self.assertIn("D. 其他（请自定义）", clarification.content)
        self.assertNotIn("5. 背景环境是什么？", clarification.content)
        self.assertIn("问题宁少勿多，最多四个", self.chat_client.calls[-1]["system"])
        self.assertIn("不得把已经能识别的问题拆到后续轮次", self.chat_client.calls[-1]["system"])
        self.assertIn("禁止输出半成品提示词", self.chat_client.calls[-1]["system"])
        self.assertIn('"status":"ready"', self.chat_client.calls[-1]["system"])

        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="竖版新品发布海报，主视觉用银色运动鞋，其余你决定，不要文字。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "竖版新品发布海报，银色运动鞋为主视觉，不含文字。",
                "prompt": "竖版新品发布海报，银色运动鞋居中，干净背景，不含文字。",
            },
            ensure_ascii=False,
        )

        ready = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )

        self.assertEqual(ready.kind, "prompt_draft")
        self.assertEqual(ready.payload["status"], "ready")
        self.assertEqual(ready.payload["reference_ids"], [])
        self.assertIn("银色运动鞋", ready.payload["prompt"])

    def test_img2img_prompt_draft_uses_selected_generation_references(self):
        workspace = self.create_workspace("产品换场景")
        assets = self.services.workspaces.add_assets(
            workspace,
            [
                ("product.png", png_bytes()),
                ("style.png", png_bytes((40, 90, 180))),
            ],
        )
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="保留产品外形，换成户外广告场景。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "保留产品外形，参考图 2 提供风格，改成户外广告场景。",
                "prompt": (
                    "参考图 1 保留产品外形；参考图 2 仅保留色彩和材质风格；"
                    "改成户外广告场景，不改变产品标志。"
                ),
            },
            ensure_ascii=False,
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
            reference_ids=(assets[0].id, assets[1].id),
        )

        self.assertEqual(draft.kind, "prompt_draft")
        self.assertEqual(draft.payload["generation_mode"], "img2img")
        self.assertEqual(draft.payload["reference_ids"], [assets[0].id, assets[1].id])
        self.assertEqual(
            [attachment.asset_id for attachment in draft.attachments],
            [assets[0].id, assets[1].id],
        )
        model_parts = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in model_parts], ["text", "image_url", "image_url"])
        system = self.chat_client.calls[-1]["system"]
        self.assertIn("当前生成模式是 img2img", system)
        self.assertIn("每张图的“必须保留”和“必须改变”", system)
        self.assertIn("参考图 1/参考图 2", system)

    def test_img2img_prompt_draft_rejects_ready_without_a_reference(self):
        workspace = self.create_workspace("缺少垫图")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="把这张产品图换成白色背景。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "产品白底图",
                "prompt": "白色背景的产品图",
            },
            ensure_ascii=False,
        )

        message = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
        )

        self.assertEqual(message.kind, "message")
        self.assertEqual(message.payload["generation_mode"], "img2img")
        self.assertEqual(message.payload["status"], "needs_clarification")
        self.assertNotIn("prompt", message.payload)
        self.assertIn("上传或选择至少一张参考图", message.content)

    def test_text2img_prompt_draft_ignores_previous_chat_attachments(self):
        workspace = self.create_workspace("文生图草稿")
        asset = self.services.workspaces.add_assets(
            workspace,
            [("history.png", png_bytes())],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="先分析这张历史参考图。",
            attachment_ids=(asset.id,),
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="text2img",
        )

        self.assertEqual(draft.kind, "prompt_draft")
        self.assertEqual(draft.payload["generation_mode"], "text2img")
        self.assertEqual(draft.payload["reference_ids"], [])
        self.assertEqual(draft.attachments, [])
        self.assertIsInstance(self.chat_client.calls[-1]["messages"][-1]["content"], str)
        self.assertIn("当前生成模式是 text2img", self.chat_client.calls[-1]["system"])

        with self.assertRaisesRegex(ServiceError, "文生图提示词草稿不能携带参考图"):
            self.services.conversations.create_prompt_draft(
                workspace,
                model_id="test-chat",
                translate_to_english=False,
                mode="text2img",
                reference_ids=(asset.id,),
            )

    def test_img2img_prompt_draft_does_not_reuse_previous_chat_attachments(self):
        workspace = self.create_workspace("重新选择垫图")
        asset = self.services.workspaces.add_assets(
            workspace,
            [("history.png", png_bytes())],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="先分析这张历史参考图。",
            attachment_ids=(asset.id,),
        )

        message = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
        )

        self.assertEqual(message.kind, "message")
        self.assertEqual(message.payload["generation_mode"], "img2img")
        self.assertEqual(message.payload["reference_ids"], [])
        self.assertEqual(message.attachments, [])
        self.assertIn("上传或选择至少一张参考图", message.content)
        self.assertIsInstance(self.chat_client.calls[-1]["messages"][-1]["content"], str)

    def test_admin_can_customize_workspace_prompts_for_chat_and_drafts(self):
        client = self.admin_client()
        config = client.get("/api/admin/chat-models").json["config"]
        self.assertNotIn("prompt_draft_model_id", config)
        self.assertIn("AI 视觉创作搭档", config["system_prompts"]["chat"])
        config["system_prompts"] = {
            "chat": "自定义基础对话规则：先准确理解用户，再提出必要问题。",
        }
        self.assertIn("当前是静态图片工作站", config["workspace_prompts"]["image"])
        config["workspace_prompts"] = {
            "image": "自定义图片规则：画面只采用一个明确的视觉中心。",
        }

        response = client.put("/api/admin/chat-models", json=config)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["config"]["system_prompts"], config["system_prompts"])
        self.assertEqual(response.json["config"]["workspace_prompts"], config["workspace_prompts"])
        image_workspace = self.create_workspace("自定义单图")
        self.services.conversations.send(
            image_workspace,
            model_id="test-chat",
            content="生成一张产品主视觉",
        )
        self.assertIn("自定义基础对话规则", self.chat_client.calls[-1]["system"])
        self.assertIn("自定义图片规则", self.chat_client.calls[-1]["system"])
        self.services.conversations.create_prompt_draft(
            image_workspace,
            model_id="test-chat",
            translate_to_english=False,
        )
        self.assertIn("自定义图片规则", self.chat_client.calls[-1]["system"])
        self.assertIn("只输出一个 JSON 对象", self.chat_client.calls[-1]["system"])

    def test_conversation_and_prompt_draft_use_selected_chat_model(self):
        config = self.admin_client().get("/api/admin/chat-models").json["config"]
        config["models"].append(
            {
                "id": "creative-chat",
                "label": "创意模型",
                "enabled": True,
                "base_url": "https://chat.example",
                "api_key": "creative-chat-key",
                "model": "gpt-creative",
                "reasoning_effort": "low",
                "review_reasoning_effort": "medium",
                "timeout_seconds": 30,
                "max_output_tokens": 1000,
            }
        )
        response = self.admin_client().put("/api/admin/chat-models", json=config)
        self.assertEqual(response.status_code, 200)
        public_models = self.user_client().get("/api/chat-models").json["models"]
        self.assertEqual(
            [model["id"] for model in public_models],
            ["test-chat", "creative-chat"],
        )

        workspace = self.create_workspace()
        self.services.conversations.send(
            workspace,
            model_id="creative-chat",
            content="电影感人物肖像",
        )
        self.assertEqual(self.chat_client.calls[-1]["model_id"], "creative-chat")
        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="creative-chat",
            translate_to_english=False,
        )
        self.assertEqual(self.chat_client.calls[-1]["model_id"], "creative-chat")
        self.assertEqual(self.chat_client.calls[-1]["model"], "gpt-creative")
        self.assertEqual(draft.provider_id, "creative-chat")
        self.assertEqual(workspace.settings["chat_model_id"], "creative-chat")

    def test_active_generation_blocks_chat_until_job_is_terminal(self):
        workspace = self.create_workspace()
        self.submit(workspace)
        with self.assertRaisesRegex(ServiceError, "图片尚未生成完成"):
            self.services.conversations.send(
                workspace,
                model_id="test-chat",
                content="继续调整画面",
            )

    def test_first_chat_message_keeps_workspace_name_and_clear_removes_transcript(
        self,
    ):
        workspace = self.create_workspace("角色设定")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="红发蓝眼的中年男性角色设定",
        )
        db.session.refresh(workspace)
        self.assertEqual(workspace.name, "角色设定")
        self.assertEqual(len(self.chat_client.calls), 1)
        self.services.workspaces.clear(workspace)
        count = db.session.scalar(
            select(func.count(ConversationMessage.id)).where(
                ConversationMessage.workspace_id == workspace.id
            )
        )
        self.assertEqual(count, 0)
