from __future__ import annotations

import json

from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import (
    ConversationMessage,
)
from imagegen.services import ServiceError
from imagegen.services.creative import (
    CREATIVE_DIRECTIONS,
    PROMPT_TEMPLATES,
    SCENE_TAG_LABELS,
    STYLE_TAG_LABELS,
)
from tests.support.platform import (
    PlatformTestCase,
    png_bytes,
)


class TestPromptDrafts(PlatformTestCase):
    def test_creative_catalog_keeps_all_source_dimensions(self):
        self.assertEqual(len(CREATIVE_DIRECTIONS), 13)
        self.assertEqual(len(STYLE_TAG_LABELS), 19)
        self.assertEqual(len(SCENE_TAG_LABELS), 10)
        self.assertEqual(len(PROMPT_TEMPLATES), 22)

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

    def test_animation_prompt_draft_gate_checks_motion_plan_with_runtime_parameters(self):
        workspace = self.create_workspace("模糊动作", kind="animation")
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="让这个角色动起来",
            attachment_ids=(master.id,),
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": ["角色要做哪种主动作：原地挥手、转身，还是由我选择一个稳定动作？"],
            },
            ensure_ascii=False,
        )

        clarification = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
            reference_ids=(master.id,),
        )

        self.assertEqual(clarification.kind, "message")
        self.assertEqual(clarification.payload["status"], "needs_clarification")
        system = self.chat_client.calls[-1]["system"]
        self.assertIn("主动作只有“动起来”等抽象描述", system)
        self.assertIn("帧数：8 帧", system)
        self.assertIn("帧率：8 FPS", system)
        self.assertIn("当前任务固定为 img2img", system)
        self.assertIn("禁止生成母图", system)
        self.assertIn('"status":"needs_clarification"', system)

    def test_animation_prompt_draft_requires_exactly_one_user_selected_master(self):
        workspace = self.create_workspace("母图约束", kind="animation")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="角色原地挥手并循环。",
        )

        with self.assertRaisesRegex(ServiceError, "固定使用一张用户指定的母图"):
            self.services.conversations.create_prompt_draft(
                workspace,
                model_id="test-chat",
                translate_to_english=False,
                mode="text2img",
            )

        calls_before_missing_master = len(self.chat_client.calls)
        with self.assertRaisesRegex(ServiceError, "必须且只能选择一张母图"):
            self.services.conversations.create_prompt_draft(
                workspace,
                model_id="test-chat",
                translate_to_english=False,
                mode="img2img",
            )
        self.assertEqual(len(self.chat_client.calls), calls_before_missing_master)

        masters = self.services.workspaces.add_assets(
            workspace,
            [
                ("master-a.png", png_bytes()),
                ("master-b.png", png_bytes((40, 90, 180))),
            ],
        )
        with self.assertRaisesRegex(ServiceError, "必须且只能选择一张母图"):
            self.services.conversations.create_prompt_draft(
                workspace,
                model_id="test-chat",
                translate_to_english=False,
                mode="img2img",
                reference_ids=tuple(asset.id for asset in masters),
            )

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
        self.assertIn(
            "帧动画工作站只能生成帧动画",
            config["workspace_prompts"]["animation"],
        )
        self.assertIn("严禁用换颜色", config["workspace_prompts"]["animation"])
        config["workspace_prompts"] = {
            "image": "自定义图片规则：画面只采用一个明确的视觉中心。",
            "animation": "自定义动画规则：角色造型和镜头必须逐帧稳定。",
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

        animation_workspace = self.create_workspace("自定义动画", kind="animation")
        self.services.conversations.send(
            animation_workspace,
            model_id="test-chat",
            content="角色转身后挥手",
        )
        self.assertIn("自定义动画规则", self.chat_client.calls[-1]["system"])
        self.assertNotIn("自定义图片规则", self.chat_client.calls[-1]["system"])
        self.assertIn("帧数：8 帧", self.chat_client.calls[-1]["system"])
        self.assertIn("帧率：8 FPS", self.chat_client.calls[-1]["system"])
        self.assertIn("总时长：1.000 秒", self.chat_client.calls[-1]["system"])
        self.assertIn("循环：末帧应自然衔接回第 1 帧", self.chat_client.calls[-1]["system"])
        master = self.services.workspaces.add_assets(
            animation_workspace,
            [("master.png", png_bytes())],
        )[0]
        self.services.conversations.create_prompt_draft(
            animation_workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
            reference_ids=(master.id,),
        )
        self.assertIn("自定义动画规则", self.chat_client.calls[-1]["system"])
        self.assertIn("帧数：8 帧", self.chat_client.calls[-1]["system"])
        self.assertIn("帧率：8 FPS", self.chat_client.calls[-1]["system"])

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
