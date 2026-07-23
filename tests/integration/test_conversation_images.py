from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timedelta, timezone

from imagegen.extensions import db
from imagegen.models import ConversationMessage, ConversationState
from imagegen.services import ServiceError
from imagegen.storage import InvalidImageError
from tests.support.platform import (
    FakeProviderFactory,
    PlatformTestCase,
    png_bytes,
    png_bytes_with_dimensions,
)


class TestConversationImages(PlatformTestCase):
    def test_analysis_only_attachment_does_not_enable_edit_recipe(self):
        workspace = self.create_workspace("图片仅分析")
        asset = self.services.workspaces.add_assets(
            workspace,
            [("analysis.png", png_bytes())],
        )[0]
        self.chat_client.reply_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "只分析版式，重新生成一张图。",
                "prompt": "生成一张新的原创极简海报",
                "reference_usage": "analysis_only",
                "reference_reason": "原图只用于分析版式。",
                "creative_direction": "poster",
                "template_id": "poster-layout-system",
                "gallery_categories": ["typography-and-posters"],
                "style_tags": ["Poster"],
                "scene_tags": ["Creative"],
                "selection_reason": "交付物是新海报。",
                "brief": {"deliverable": "原创海报"},
                "hard_checks": ["输出是新的原创海报"],
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )

        _user, assistant = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="只分析这张图的版式，不要把它作为生图垫图。",
            attachment_ids=(asset.id,),
        )

        self.assertEqual(assistant.payload["generation_mode"], "text2img")
        self.assertEqual(assistant.payload["reference_usage"], "analysis_only")
        self.assertEqual(assistant.payload["reference_ids"], [])
        self.assertEqual(assistant.payload["edit_recipe_id"], "")

    def test_historical_chat_images_are_sent_again_on_a_later_turn(self):
        workspace = self.create_workspace("历史图片上下文")
        content = png_bytes((220, 35, 45))
        asset = self.services.workspaces.add_assets(workspace, [("history.png", content)])[0]
        response = {
            "status": "ready",
            "summary_zh": "保留历史参考图的主体",
            "prompt": "保留历史参考图的主体并调整背景",
            "creative_direction": "other",
            "template_id": "custom",
            "style_tags": [],
            "scene_tags": [],
            "selection_reason": "测试历史图片上下文。",
            "brief": {"deliverable": "图片"},
            "hard_checks": ["主体保持"],
            "quality_hint": "low",
        }
        self.chat_client.reply_content = json.dumps(response, ensure_ascii=False)

        _first_user, first_assistant = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="先参考这张图设计主体",
            attachment_ids=(asset.id,),
        )
        self.assertEqual(first_assistant.payload["generation_mode"], "img2img")
        self.assertEqual(first_assistant.payload["edit_recipe_id"], "precision-edit")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="现在只调整背景，主体保持不变",
        )

        follow_up_context = self.chat_client.calls[-1]["messages"]
        historical_images = [
            part
            for message in follow_up_context
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
            if part.get("type") == "image_url"
        ]
        self.assertTrue(historical_images)
        self.assertTrue(
            historical_images[0]["image_url"]["url"].endswith(
                base64.b64encode(content).decode("ascii")
            )
        )
        self.assertIn("先参考这张图设计主体", json.dumps(follow_up_context, ensure_ascii=False))

    def test_completed_generation_is_available_to_the_next_chat_turn(self):
        workspace = self.create_workspace("生成结果上下文")
        prompt = "红色背景上的几何海报"
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content=f"请生成：{prompt}",
        )
        job = self.submit(workspace, prompt=prompt)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)

        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="基于刚才的成品把文字放大",
        )

        follow_up_context = self.chat_client.calls[-1]["messages"]
        serialized = json.dumps(follow_up_context, ensure_ascii=False)
        self.assertIn(prompt, serialized)
        self.assertIn("历史生成结果", serialized)
        result_image = [
            part
            for message in follow_up_context
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
            if part.get("type") == "image_url"
            and base64.b64encode(png_bytes()).decode("ascii") in part["image_url"]["url"]
        ]
        self.assertTrue(result_image)

    def test_overflow_truncates_old_text_without_ai_summary_or_losing_images(self):
        workspace = self.create_workspace("直接截断上下文")
        reference_content = png_bytes((220, 35, 45))
        reference = self.services.workspaces.add_assets(
            workspace,
            [("historical-reference.png", reference_content)],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="这张图是必须保留的历史主体参考",
            attachment_ids=(reference.id,),
        )

        prompt = "必须保留的历史生成成品"
        job = self.submit(workspace, prompt=prompt)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)

        db.session.add_all(
            ConversationMessage(
                workspace_id=workspace.id,
                role="user" if index % 2 == 0 else "assistant",
                kind="message",
                content=f"普通历史-{index}-" + "旧内容" * 500,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
            )
            for index in range(20)
        )
        db.session.commit()

        config = self.admin_client().get("/api/admin/chat-models").json["config"]
        config["context"] = {"max_context_tokens": 6000}
        response = self.admin_client().put("/api/admin/chat-models", json=config)
        self.assertEqual(response.status_code, 200)

        calls_before = len(self.chat_client.calls)
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="继续处理当前方案",
        )

        self.assertEqual(len(self.chat_client.calls), calls_before + 1)
        context = self.chat_client.calls[-1]["messages"]
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("普通历史-0-", serialized)
        self.assertIn("普通历史-19-", serialized)
        self.assertIn(prompt, serialized)
        images = [
            part["image_url"]["url"]
            for message in context
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
            if part.get("type") == "image_url"
        ]
        self.assertEqual(len(images), 2)
        self.assertTrue(
            any(url.endswith(base64.b64encode(reference_content).decode("ascii")) for url in images)
        )
        state = db.session.get(ConversationState, workspace.id)
        self.assertEqual(state.summary, "")
        self.assertEqual(state.summary_through_message_id, "")
        self.assertLessEqual(state.estimated_context_tokens, 6000)

    def test_chat_multiple_attachments_are_sent_persisted_and_cannot_cross_workspaces(self):
        workspace = self.create_workspace()
        other_workspace = self.create_workspace("其他工作站")
        first_content = png_bytes((220, 35, 45))
        second_content = png_bytes((25, 80, 220))
        assets = self.services.workspaces.add_assets(
            workspace,
            [("subject.png", first_content), ("layout.png", second_content)],
        )
        user_message, _assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="同时参考这两张图",
            attachment_ids=(assets[1].id, assets[0].id),
        )
        self.assertEqual(
            [item.asset_id for item in user_message.attachments],
            [assets[1].id, assets[0].id],
        )
        model_parts = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in model_parts], ["text", "image_url", "image_url"])
        self.assertTrue(
            model_parts[1]["image_url"]["url"].endswith(
                base64.b64encode(second_content).decode("ascii")
            )
        )
        self.assertTrue(
            model_parts[2]["image_url"]["url"].endswith(
                base64.b64encode(first_content).decode("ascii")
            )
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
            reference_ids=(assets[1].id, assets[0].id),
        )
        self.assertEqual(draft.payload["generation_mode"], "img2img")
        self.assertEqual(draft.payload["reference_ids"], [assets[1].id, assets[0].id])
        draft_parts = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in draft_parts], ["text", "image_url", "image_url"])

        with self.assertRaisesRegex(ServiceError, "参考图不存在"):
            self.services.conversations.send(
                other_workspace,
                model_id="test-chat",
                content="错误引用",
                attachment_ids=(assets[0].id,),
            )

    def test_img2img_follow_up_keeps_the_latest_chat_reference(self):
        workspace = self.create_workspace("连续垫图对话")
        settings = dict(workspace.settings)
        settings["mode"] = "img2img"
        workspace.settings = settings
        db.session.commit()
        reference = self.services.workspaces.add_assets(
            workspace,
            [("previous-result.png", png_bytes())],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="基于这张图继续修改",
            attachment_ids=(reference.id,),
            generation_mode="img2img",
            generation_reference_ids=(reference.id,),
        )

        follow_up, _assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="背景再简洁一点，其他保持不变",
            generation_mode="img2img",
            generation_reference_ids=(reference.id,),
        )

        self.assertEqual(follow_up.attachments, [])
        model_content = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in model_content], ["text", "image_url"])
        self.assertIn("当前生成模式是 img2img", self.chat_client.calls[-1]["system"])



    def test_clarification_follow_up_inherits_chat_attachments_without_explicit_generation_refs(self):
        workspace = self.create_workspace("澄清后继承聊天垫图")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("chat-pad.png", png_bytes((44, 120, 90)))],
        )[0]
        self.chat_client.reply_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": ["角色是否保留？\nA. 删除（推荐）\nB. 保留\nC. 弱化\nD. 其他（请自定义）"],
                "creative_direction": "poster",
            },
            ensure_ascii=False,
        )
        _first_user, clarification = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="基于这张垫图调整风格和调色",
            attachment_ids=(reference.id,),
        )
        self.assertEqual(clarification.payload["status"], "needs_clarification")

        self.chat_client.reply_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "删除左侧角色并保持垫图冷灰雪景色调",
                "prompt": "参考图 1 保留雪山木屋冷灰色调，删除左侧角色",
                "creative_direction": "poster",
                "template_id": "custom",
                "style_tags": [],
                "scene_tags": [],
                "selection_reason": "用户确认删除角色并延续垫图风格。",
                "brief": {"deliverable": "海报"},
                "hard_checks": ["无左侧角色"],
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )
        follow_up, assistant = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="A",
        )

        self.assertEqual(follow_up.attachments, [])
        self.assertEqual(assistant.kind, "prompt_draft")
        self.assertEqual(assistant.payload["reference_ids"], [reference.id])
        self.assertEqual(assistant.payload["generation_mode"], "img2img")
        model_content = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertIn("image_url", [part["type"] for part in model_content])

    def test_clarification_follow_up_inherits_latest_pad_images(self):
        workspace = self.create_workspace("澄清后自动垫图")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("pad.png", png_bytes((12, 88, 160)))],
        )[0]
        self.chat_client.reply_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": [
                    "左侧角色是否保留？\nA. 删除（推荐）\nB. 保留\nC. 弱化\nD. 其他（请自定义）",
                    "最终画幅采用哪一种？\nA. 1:1（推荐）\nB. 16:9\nC. 4:5\nD. 其他（请自定义）",
                ],
                "creative_direction": "poster",
            },
            ensure_ascii=False,
        )
        first_user, clarification = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="基于最新的垫图帮我分析，要风格调调和垫图一致",
            attachment_ids=(reference.id,),
            generation_mode="img2img",
            generation_reference_ids=(reference.id,),
        )
        self.assertEqual(clarification.payload["status"], "needs_clarification")
        self.assertEqual(first_user.payload["generation_reference_ids"], [reference.id])
        self.assertEqual(clarification.payload.get("reference_ids"), [reference.id])
        self.assertEqual(
            [attachment.asset_id for attachment in clarification.attachments],
            [reference.id],
        )

        self.chat_client.reply_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "删除左侧角色并改为 1:1 海报",
                "prompt": "参考图 1 保留雪山木屋枯树月光冷灰色调，删除左侧角色，输出 1:1 海报",
                "creative_direction": "poster",
                "template_id": "custom",
                "style_tags": [],
                "scene_tags": [],
                "selection_reason": "用户确认删除角色并使用 1:1 画幅。",
                "brief": {"deliverable": "海报"},
                "hard_checks": ["无左侧角色"],
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )
        follow_up, assistant = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="A A",
        )

        self.assertEqual(follow_up.attachments, [])
        self.assertEqual(assistant.kind, "prompt_draft")
        self.assertEqual(assistant.payload["generation_mode"], "img2img")
        self.assertEqual(assistant.payload["reference_ids"], [reference.id])
        model_content = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in model_content], ["text", "image_url"])
        self.assertIn("当前生成模式是 img2img", self.chat_client.calls[-1]["system"])

    def test_chat_requires_explicit_img2img_references(self):
        workspace = self.create_workspace("显式垫图")
        settings = dict(workspace.settings)
        settings["mode"] = "img2img"
        workspace.settings = settings
        db.session.commit()
        self.chat_client.reply_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "基于母图修改背景",
                "prompt": "保留主体，仅修改背景",
                "creative_direction": "other",
                "template_id": "custom",
                "style_tags": [],
                "scene_tags": [],
                "selection_reason": "测试",
                "brief": {"deliverable": "图片"},
                "hard_checks": ["主体保留"],
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )

        _user, assistant = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="继续修改，但这一轮没有选择垫图",
            generation_mode="img2img",
        )

        self.assertEqual(assistant.kind, "message")
        self.assertEqual(assistant.payload["generation_mode"], "img2img")
        self.assertIn("上传或选择至少一张参考图", assistant.content)

    def test_generation_reference_and_chat_attachment_are_both_sent_to_model(self):
        workspace = self.create_workspace("垫图与分析图")
        assets = self.services.workspaces.add_assets(
            workspace,
            [("generation.png", png_bytes()), ("analysis.png", png_bytes((40, 90, 180)))],
        )
        self.chat_client.reply_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": ["请确认要修改的区域"],
                "creative_direction": "other",
            },
            ensure_ascii=False,
        )
        _user, _assistant = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="用第二张图分析布局，第一张图作为生图垫图",
            attachment_ids=(assets[1].id,),
            generation_mode="img2img",
            generation_reference_ids=(assets[0].id,),
        )
        model_parts = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in model_parts], ["text", "image_url", "image_url"])
        self.assertEqual(_assistant.payload["generation_mode"], "img2img")
        self.assertEqual(_assistant.payload["reference_ids"], [assets[0].id])

    def test_workspace_reference_limit_allows_delete_then_custom_add(self):
        workspace = self.create_workspace()
        assets = self.services.workspaces.add_assets(
            workspace,
            [(f"reference-{index}.png", png_bytes((index * 10, 80, 160))) for index in range(20)],
        )
        with self.assertRaisesRegex(ServiceError, "最多保留 20 张参考图"):
            self.services.workspaces.add_assets(
                workspace,
                [("too-many.png", png_bytes((250, 250, 20)))],
            )

        self.services.workspaces.remove_asset(workspace, assets[0].id)
        replacement = self.services.workspaces.add_assets(
            workspace,
            [("custom-replacement.png", png_bytes((250, 250, 20)))],
        )
        self.assertEqual(len(replacement), 1)

    def test_reference_upload_rejects_oversized_and_incomplete_images(self):
        workspace = self.create_workspace()
        client = self.user_client()
        for content in (
            png_bytes_with_dimensions(9000, 100),
            png_bytes_with_dimensions(8192, 8192),
            png_bytes()[:40],
        ):
            response = client.post(
                f"/api/workspaces/{workspace.id}/assets",
                data={"references": (io.BytesIO(content), "invalid.png")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["code"], "invalid_image")

        with self.assertRaises(InvalidImageError):
            self.app.extensions["image_storage"].inspect(png_bytes()[:40])

    def test_chat_api_uploads_multiple_references_and_delete_preserves_history(self):
        workspace = self.create_workspace()
        client = self.user_client()
        response = client.post(
            f"/api/workspaces/{workspace.id}/assets",
            data={
                "references": [
                    (io.BytesIO(png_bytes((220, 35, 45))), "subject.png"),
                    (io.BytesIO(png_bytes((25, 80, 220))), "layout.png"),
                ]
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        assets = response.json["assets"]
        self.assertEqual([asset["name"] for asset in assets], ["subject.png", "layout.png"])

        response = client.post(
            f"/api/workspaces/{workspace.id}/messages",
            json={
                "model_id": "test-chat",
                "content": "融合人物与版式参考",
                "attachment_ids": [asset["id"] for asset in assets],
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            [asset["id"] for asset in response.json["messages"][0]["attachments"]],
            [asset["id"] for asset in assets],
        )

        response = client.delete(f"/api/workspaces/{workspace.id}/assets/{assets[0]['id']}")
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        workspaces = client.get("/api/workspaces").json["workspaces"]
        current = next(item for item in workspaces if item["id"] == workspace.id)
        self.assertEqual([asset["id"] for asset in current["assets"]], [assets[1]["id"]])
        messages = client.get(f"/api/workspaces/{workspace.id}/messages").json["messages"]
        self.assertEqual(
            [asset["id"] for asset in messages[0]["attachments"]],
            [asset["id"] for asset in assets],
        )
