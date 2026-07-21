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
    PROMPT_TEMPLATES,
    SCENE_TAG_LABELS,
    STYLE_TAG_LABELS,
    creative_direction_dicts,
)
from tests.support.platform import (
    PlatformTestCase,
    png_bytes,
)


class TestPromptDrafts(PlatformTestCase):
    def test_creative_catalog_keeps_all_source_dimensions(self):
        self.assertEqual(len(CREATIVE_DIRECTIONS), 15)
        self.assertEqual(len(STYLE_TAG_LABELS), 29)
        self.assertEqual(len(SCENE_TAG_LABELS), 11)
        self.assertEqual(len(PROMPT_TEMPLATES), 32)

    def test_game_directions_are_first_concrete_choices_and_have_production_contracts(self):
        directions = creative_direction_dicts()
        self.assertEqual([item["id"] for item in directions[:3]], ["auto", "game_ui", "game_art"])
        game_templates = [
            template
            for template in PROMPT_TEMPLATES
            if template.direction_id in {"game_ui", "game_art"}
        ]
        self.assertEqual(len(game_templates), 10)
        self.assertTrue(all(template.case_refs for template in game_templates))
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
        self.assertIn("ui-screenshot-system", chat_system)
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "运动鞋新品发布海报，标题 AIR ZERO。",
                "prompt": '3:4 vertical poster with exact title "AIR ZERO".',
                "creative_direction": "poster",
                "template_id": "poster-layout-system",
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
        self.assertEqual(draft.payload["style_labels"], ["海报"])
        self.assertEqual(draft.payload["scene_labels"], ["商业", "社媒"])
        self.assertIn("主视觉与标题层级", draft.payload["selection_reason"])
        self.assertEqual(draft.payload["sources"][1]["url"], "https://gpt-image2.canghe.ai/")
        system = self.chat_client.calls[-1]["system"]
        self.assertIn("交付物分类 → 视觉风格 → 使用场景 → 最近模板", system)
        self.assertIn("ui-screenshot-system", system)
        self.assertIn("concept-product-breakdown", system)
        self.assertIn("若用户从外部图库复制提示词", system)

        locked = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            creative_direction_id="product",
        )
        self.assertEqual(locked.payload["creative_direction"], "product")
        self.assertEqual(locked.payload["template_id"], "custom")

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
        self.assertEqual(len(draft.payload["hard_checks"]), 6)
        self.assertIn("gallery-gaming.md", draft.payload["sources"][2]["references"]["gaming"])
        self.assertIn("game-ui-gameplay-hud", self.chat_client.calls[-1]["system"])
        self.assertIn("平台、目标画布、屏幕状态和安全区", self.chat_client.calls[-1]["system"])

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
        self.assertEqual(draft.payload["brief"]["reference_plan"][0]["role"], "")

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
