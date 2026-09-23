from __future__ import annotations

import io
import json
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from imagegen.config.channels import ChannelRegistry
from imagegen.extensions import db
from imagegen.models import (
    GenerationAttempt,
    GenerationItem,
    GenerationJob,
    User,
    WalletLedger,
    utcnow,
)
from imagegen.services import ServiceError
from tests.support.platform import (
    CHANNEL_CONFIG,
    BlockingProviderFactory,
    FakeProviderFactory,
    HoldingExecutor,
    PlatformTestCase,
    mask_png_bytes,
    png_bytes,
)


class TestGenerations(PlatformTestCase):
    def test_submit_uses_one_channel_selected_model_and_reserves_batch_price(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=3)
        user = db.session.get(User, self.user.id)
        self.assertEqual(job.channel_id, "test")
        self.assertEqual(job.model, "model-b")
        self.assertEqual(job.requested_count, 3)
        self.assertEqual(job.reserved_rmb, Decimal("3.7500"))
        self.assertEqual(user.reserved_rmb, Decimal("3.7500"))
        self.assertEqual(len(job.items), 3)

    def test_generation_api_defaults_to_final_auto_quality(self):
        workspace = self.create_workspace("默认自动质量")
        self.assertEqual(workspace.settings["generation_stage"], "final")
        self.assertEqual(workspace.settings["quality"], "auto")

        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "默认使用自动质量",
            },
        )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(response.json["job"]["quality"], "auto")
        self.assertEqual(response.json["job"]["moderation"], "auto")
        self.assertEqual(response.json["job"]["workflow"]["generation_stage"], "final")
        self.assertEqual(response.json["job"]["workflow"]["moderation"], "auto")

    def test_low_moderation_is_persisted_only_for_gpt_image_2_series(self):
        workspace = self.create_workspace("低强度审核")
        with self.assertRaisesRegex(ServiceError, "仅支持 GPT Image 2"):
            self.submit(workspace, moderation="low")
        with self.assertRaisesRegex(ServiceError, "内容审核级别无效"):
            self.submit(workspace, moderation="off")

        for model_id in ("gpt-image-2", "gpt-image-2.5-flare"):
            with self.subTest(model_id=model_id):
                self.channel_path.write_text(
                    CHANNEL_CONFIG.replace("id: model-b", f"id: {model_id}"), encoding="utf-8"
                )
                self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
                response = self.user_client().post(
                    "/api/generations",
                    json={
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": model_id,
                        "prompt": "插画风景",
                        "moderation": "low",
                    },
                )
                self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
                self.assertEqual(response.json["job"]["moderation"], "low")
                self.assertEqual(response.json["job"]["workflow"]["moderation"], "low")
                db.session.refresh(workspace)
                self.assertEqual(workspace.settings["moderation"], "low")

    def test_generation_api_infers_mode_from_reference_selection(self):
        client = self.user_client()
        text_workspace = self.create_workspace("自动文生图")
        text_response = client.post(
            "/api/generations",
            json={
                "workspace_id": text_workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "img2img",
                "prompt": "不使用参考图",
                "reference_ids": [],
            },
        )

        self.assertEqual(text_response.status_code, 202, text_response.get_data(as_text=True))
        self.assertEqual(text_response.json["job"]["mode"], "text2img")
        self.assertEqual(text_response.json["job"]["references"], [])

        image_workspace = self.create_workspace("自动垫图生图")
        reference = self.services.workspaces.add_assets(
            image_workspace,
            [("reference.png", png_bytes())],
        )[0]
        image_response = client.post(
            "/api/generations",
            json={
                "workspace_id": image_workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "使用参考图",
                "reference_ids": [reference.id],
            },
        )

        self.assertEqual(image_response.status_code, 202, image_response.get_data(as_text=True))
        self.assertEqual(image_response.json["job"]["mode"], "img2img")
        self.assertEqual(
            [item["id"] for item in image_response.json["job"]["references"]],
            [reference.id],
        )

    def test_masked_generation_persists_one_target_and_reaches_worker(self):
        workspace = self.create_workspace("局部重绘")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("source.png", png_bytes())],
        )[0]
        payload = {
            "workspace_id": workspace.id,
            "channel_id": "test",
            "model": "model-b",
            "prompt": "将框选区域替换为白色陶瓷杯，保持其他内容不变",
            "reference_ids": [reference.id],
            "mask_target_asset_id": reference.id,
        }

        response = self.user_client().post(
            "/api/generations",
            data={
                "payload": json.dumps(payload, ensure_ascii=False),
                "mask": (io.BytesIO(mask_png_bytes()), "mask.png"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        serialized = response.json["job"]
        self.assertTrue(serialized["has_mask"])
        self.assertEqual(serialized["mask_target_asset_id"], reference.id)
        self.assertEqual([asset["id"] for asset in serialized["references"]], [reference.id])
        job = db.session.get(GenerationJob, serialized["id"])
        self.assertEqual(job.mask_target_asset_id, reference.id)
        self.assertEqual(job.mask_width, 64)
        self.assertEqual(job.mask_height, 48)
        stored_mask = self.app.extensions["image_storage"].read_bytes(job.mask_storage_path)

        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        item_id = serialized["items"][0]["id"]
        self.assertTrue(worker._claim(item_id, channel))
        worker._process_item(item_id)
        self.assertEqual(worker.providers.adapter.request.mask.content, stored_mask)
        self.assertEqual(len(worker.providers.adapter.request.references), 1)

    def test_masked_generation_rejects_stale_target_and_empty_selection(self):
        workspace = self.create_workspace("局部重绘校验")
        references = self.services.workspaces.add_assets(
            workspace,
            [
                ("first.png", png_bytes()),
                ("second.png", png_bytes((90, 80, 170))),
            ],
        )
        client = self.user_client()

        stale = client.post(
            "/api/generations",
            data={
                "payload": json.dumps(
                    {
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": "model-b",
                        "prompt": "替换框选区域",
                        "reference_ids": [references[0].id],
                        "mask_target_asset_id": references[1].id,
                    }
                ),
                "mask": (io.BytesIO(mask_png_bytes()), "mask.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["code"], "mask_target_conflict")

        opaque = client.post(
            "/api/generations",
            data={
                "payload": json.dumps(
                    {
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": "model-b",
                        "prompt": "替换框选区域",
                        "reference_ids": [references[0].id],
                        "mask_target_asset_id": references[0].id,
                    }
                ),
                "mask": (io.BytesIO(png_bytes()), "opaque.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(opaque.status_code, 422)
        self.assertEqual(opaque.json["code"], "invalid_mask")
        self.assertIn("尚未选择", opaque.json["error"])

    def test_generation_mask_metadata_constraint_rejects_partial_record(self):
        workspace = self.create_workspace("局部重绘元数据约束")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("source.png", png_bytes())],
        )[0]
        job = self.submit(workspace)
        job.mask_target_asset_id = reference.id
        job.mask_storage_path = (
            f"users/{self.user.id}/workspaces/{workspace.id}/generations/{job.id}/mask.png"
        )
        job.mask_sha256 = "a" * 64
        job.mask_width = 64
        job.mask_height = 48

        with self.assertRaises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_masked_generation_reports_missing_reference_file(self):
        workspace = self.create_workspace("局部重绘文件校验")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("source.png", png_bytes())],
        )[0]
        self.app.extensions["image_storage"].delete(reference.storage_path)

        response = self.user_client().post(
            "/api/generations",
            data={
                "payload": json.dumps(
                    {
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": "model-b",
                        "prompt": "替换框选区域",
                        "reference_ids": [reference.id],
                        "mask_target_asset_id": reference.id,
                    }
                ),
                "mask": (io.BytesIO(mask_png_bytes()), "mask.png"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "reference_unavailable")
        self.assertEqual(db.session.scalar(select(func.count(GenerationJob.id))), 0)

    def test_masked_generation_workspace_delete_removes_mask_file(self):
        workspace = self.create_workspace("局部重绘清理")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("source.png", png_bytes())],
        )[0]
        response = self.user_client().post(
            "/api/generations",
            data={
                "payload": json.dumps(
                    {
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": "model-b",
                        "prompt": "替换框选区域",
                        "reference_ids": [reference.id],
                        "mask_target_asset_id": reference.id,
                    }
                ),
                "mask": (io.BytesIO(mask_png_bytes()), "mask.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        job = db.session.get(GenerationJob, response.json["job"]["id"])
        mask_path = job.mask_storage_path

        self.services.workspaces.delete(workspace)

        self.assertIsNone(db.session.get(GenerationJob, job.id))
        self.assertFalse((self.app.extensions["image_storage"].root / mask_path).exists())

    def test_masked_generation_rejects_channel_without_mask_capability(self):
        workspace = self.create_workspace("局部重绘渠道能力")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("source.png", png_bytes())],
        )[0]
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("supports_mask: true", "supports_mask: false"),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))

        response = self.user_client().post(
            "/api/generations",
            data={
                "payload": json.dumps(
                    {
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": "model-b",
                        "prompt": "替换框选区域",
                        "reference_ids": [reference.id],
                        "mask_target_asset_id": reference.id,
                    }
                ),
                "mask": (io.BytesIO(mask_png_bytes()), "mask.png"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json["code"], "mask_not_supported")
        self.assertEqual(db.session.scalar(select(func.count(GenerationJob.id))), 0)

    def test_generation_api_keeps_quality_independent_from_reviewed_stage(self):
        client = self.user_client()
        for stage, quality in (
            ("draft", "high"),
            ("refine", "low"),
            ("final", "medium"),
        ):
            with self.subTest(stage=stage):
                workspace = self.create_workspace(f"{stage} 阶段")
                prompt = f"{stage} 阶段海报"
                draft = self.create_ready_prompt_draft(
                    workspace,
                    prompt=prompt,
                    creative_direction_id="poster",
                    template_id="poster-layout-system",
                )
                response = client.post(
                    "/api/generations",
                    json={
                        "workspace_id": workspace.id,
                        "channel_id": "test",
                        "model": "model-b",
                        "mode": "text2img",
                        "prompt": prompt,
                        "prompt_draft_id": draft.id,
                        "generation_stage": stage,
                        "quality": quality,
                    },
                )

                self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
                job = response.json["job"]
                self.assertEqual(job["quality"], quality)
                self.assertEqual(job["workflow"]["generation_stage"], stage)
                self.assertEqual(job["workflow"]["prompt_draft_id"], draft.id)
                self.assertEqual(job["workflow"]["creative_direction_id"], "poster")
                self.assertEqual(job["workflow"]["template_id"], "poster-layout-system")
                self.assertEqual(job["workflow"]["template_label"], "海报排版系统")
                self.assertEqual(job["workflow"]["style_tags"], ["Poster"])
                self.assertEqual(job["workflow"]["gallery_categories"], ["typography-and-posters"])
                self.assertEqual(job["workflow"]["gallery_category_labels"], ["排版与海报"])
                self.assertEqual(job["workflow"]["gallery_case_ranges"], ["skill:33-45"])
                self.assertNotIn("canvas_request", job["workflow"])
                self.assertNotIn("canvas_resolution", job["workflow"])
                db.session.refresh(workspace)
                self.assertEqual(workspace.settings["generation_stage"], stage)
                self.assertEqual(workspace.settings["quality"], quality)

    def test_generation_quality_options_follow_gpt_image_model(self):
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("id: model-b", "id: gpt-image-2"), encoding="utf-8"
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))

        for quality in ("auto", "low", "medium", "high"):
            with self.subTest(model="gpt-image-2", quality=quality):
                workspace = self.create_workspace(f"GPT Image 2 {quality}")
                job = self.submit(workspace, model="gpt-image-2", quality=quality)
                self.assertEqual(job.quality, quality)
                self.assertEqual(workspace.settings["quality"], quality)

        for quality in ("xhigh", "max"):
            with self.subTest(model="gpt-image-2", quality=quality):
                workspace = self.create_workspace(f"GPT Image 2 拒绝 {quality}")
                with self.assertRaisesRegex(ServiceError, "仅支持 GPT Image 2.5"):
                    self.submit(workspace, model="gpt-image-2", quality=quality)

        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("id: model-b", "id: gpt-image-2.5-flare"),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
        for quality in ("xhigh", "max"):
            with self.subTest(model="gpt-image-2.5-flare", quality=quality):
                workspace = self.create_workspace(f"GPT Image 2.5 {quality}")
                job = self.submit(workspace, model="gpt-image-2.5-flare", quality=quality)
                self.assertEqual(job.quality, quality)
                self.assertEqual(workspace.settings["quality"], quality)

        workspace = self.create_workspace("非法质量")
        with self.assertRaisesRegex(ServiceError, "生成质量无效"):
            self.submit(workspace, model="gpt-image-2.5-flare", quality="ultra")

    def test_img2img_style_contract_reaches_worker_provider_request(self):
        workspace = self.create_workspace("风格契约闭环")
        reference = self.services.workspaces.add_assets(
            workspace,
            [("product.png", png_bytes())],
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="保留产品主体，改成克制的冷灰商业摄影风格。",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "保留产品主体并使用冷灰商业摄影风格。",
                "prompt": "保留产品主体，改成冷灰商业摄影风格。",
                "reference_usage": "generation",
                "reference_reason": "参考图用于保留产品主体。",
                "creative_direction": "product",
                "template_id": "product-commerce-visual",
                "style_tags": ["Product"],
                "brief": {
                    "deliverable": "商品图",
                    "style": "克制商业摄影，冷灰金属材质与柔和侧光",
                    "reference_plan": [
                        {
                            "image_number": 1,
                            "role": "待编辑原图",
                            "preserve": ["产品主体"],
                            "change": ["背景风格"],
                        }
                    ],
                    "preserve": ["产品主体"],
                    "change": ["背景风格"],
                },
                "hard_checks": ["产品主体保持", "背景风格完成替换"],
            },
            ensure_ascii=False,
        )
        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode="img2img",
            reference_ids=(reference.id,),
        )

        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "img2img",
                "prompt": draft.payload["prompt"],
                "prompt_draft_id": draft.id,
                "reference_ids": [reference.id],
            },
        )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        queued_prompt = response.json["job"]["items"][0]["prompt"]
        self.assertIn('"target_visual_style"', queued_prompt)
        self.assertIn('"Product"', queued_prompt)
        self.assertIn("克制商业摄影", queued_prompt)

        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        item_id = response.json["job"]["items"][0]["id"]
        self.assertTrue(worker._claim(item_id, channel))
        worker._process_item(item_id)
        self.assertEqual(worker.providers.adapter.request.prompt, queued_prompt)

    def test_explore_strategy_persists_distinct_effective_prompts(self):
        workspace = self.create_workspace("受控探索")
        prompt = "科幻产品海报"
        draft = self.create_ready_prompt_draft(workspace, prompt=prompt)
        draft.payload = {
            **draft.payload,
            "exploration_plan": [
                {"label": "中心层级", "delta": ["主体采用中心构图"]},
                {"label": "非对称留白", "delta": ["右侧保留呼吸空间"]},
                {"label": "材质近景", "delta": ["镜头更接近主体"]},
            ],
        }
        db.session.commit()

        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": prompt,
                "prompt_draft_id": draft.id,
                "generation_strategy": "explore",
                "batch_count": 3,
            },
        )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = response.json["job"]
        prompts = [item["prompt"] for item in job["items"]]
        self.assertEqual(len(set(prompts)), 3)
        self.assertEqual(
            [item["label"] for item in job["workflow"]["variant_plan"]],
            ["中心层级", "非对称留白", "材质近景"],
        )
        saved_items = list(
            db.session.scalars(select(GenerationItem).where(GenerationItem.job_id == job["id"]))
        )
        self.assertEqual([item.prompt for item in saved_items], prompts)

    def test_series_strategy_keeps_anchor_first_and_repeats_contract(self):
        workspace = self.create_workspace("系列延续")
        assets = self.services.workspaces.add_assets(
            workspace,
            [("anchor.png", png_bytes()), ("palette.png", png_bytes((40, 90, 180)))],
        )
        contract = {
            "identity_anchors": ["同一产品轮廓"],
            "visual_language": ["电影感商业摄影"],
            "palette_materials": ["冷蓝金属"],
            "composition_rules": ["主体保持左侧三分位"],
            "must_preserve": ["品牌标志位置"],
            "allowed_changes": ["场景和动作"],
        }
        self.services.workspaces.set_series_anchor(
            workspace,
            asset_id=assets[0].id,
            source_item_id="b" * 32,
            contract=contract,
        )
        prompt = "系列第二张产品海报"
        draft = self.create_ready_prompt_draft(
            workspace,
            prompt=prompt,
            mode="img2img",
            reference_ids=(assets[0].id, assets[1].id),
        )

        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "img2img",
                "prompt": prompt,
                "prompt_draft_id": draft.id,
                "generation_strategy": "series",
                "batch_count": 2,
                "reference_ids": [assets[1].id, assets[0].id],
            },
        )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = response.json["job"]
        self.assertEqual([asset["id"] for asset in job["references"]], [assets[0].id, assets[1].id])
        self.assertEqual(job["workflow"]["generation_strategy"], "series")
        self.assertEqual(job["workflow"]["series_contract"], contract)
        self.assertEqual(job["items"][0]["prompt"], job["items"][1]["prompt"])
        self.assertIn("系列一致性契约", job["items"][0]["prompt"])

    def test_removing_or_clearing_series_anchor_resets_series_settings(self):
        workspace = self.create_workspace("系列状态清理")
        asset = self.services.workspaces.add_assets(workspace, [("anchor.png", png_bytes())])[0]
        self.services.workspaces.set_series_anchor(
            workspace,
            asset_id=asset.id,
            source_item_id="c" * 32,
            contract={"identity_anchors": ["主体"]},
        )

        self.services.workspaces.remove_asset(workspace, asset.id)
        db.session.refresh(workspace)
        self.assertEqual(workspace.settings["generation_strategy"], "sample")
        self.assertEqual(workspace.settings["series_anchor"], {})
        self.assertEqual(workspace.settings["reference_ids"], [])

        replacement = self.services.workspaces.add_assets(
            workspace, [("anchor-2.png", png_bytes())]
        )[0]
        self.services.workspaces.set_series_anchor(
            workspace,
            asset_id=replacement.id,
            source_item_id="d" * 32,
            contract={"identity_anchors": ["主体"]},
        )
        cleared = self.services.workspaces.clear(workspace)
        self.assertEqual(cleared.settings["generation_strategy"], "sample")
        self.assertEqual(cleared.settings["series_anchor"], {})
        self.assertEqual(cleared.settings["reference_ids"], [])

    def test_generation_api_requires_explicit_canvas_conflict_resolution(self):
        client = self.user_client()
        prompt = "2048×1152 横屏画面"

        unresolved_workspace = self.create_workspace("未处理画幅冲突")
        unresolved_draft = self.create_ready_prompt_draft(
            unresolved_workspace,
            prompt=prompt,
            canvas_request={"width": 2048, "height": 1152, "aspect_ratio": "16:9"},
        )
        base_payload = {
            "workspace_id": unresolved_workspace.id,
            "channel_id": "test",
            "model": "model-b",
            "mode": "text2img",
            "prompt": prompt,
            "prompt_draft_id": unresolved_draft.id,
            "size": "1024x1024",
        }
        unresolved = client.post("/api/generations", json=base_payload)
        self.assertEqual(unresolved.status_code, 409)
        self.assertEqual(unresolved.json["code"], "prompt_canvas_conflict")
        wrong_choice = client.post(
            "/api/generations",
            json={**base_payload, "canvas_resolution": "conversation"},
        )
        self.assertEqual(wrong_choice.status_code, 409)
        self.assertEqual(wrong_choice.json["code"], "prompt_canvas_conflict")

        panel_workspace = self.create_workspace("保留面板画幅")
        panel_draft = self.create_ready_prompt_draft(
            panel_workspace,
            prompt=prompt,
            canvas_request={"width": 2048, "height": 1152, "aspect_ratio": "16:9"},
        )
        panel = client.post(
            "/api/generations",
            json={
                **base_payload,
                "workspace_id": panel_workspace.id,
                "prompt_draft_id": panel_draft.id,
                "canvas_resolution": "panel",
            },
        )
        self.assertEqual(panel.status_code, 202, panel.get_data(as_text=True))
        self.assertEqual(panel.json["job"]["workflow"]["canvas_resolution"], "panel")
        self.assertEqual(
            panel.json["job"]["workflow"]["canvas_request"],
            {"width": 2048, "height": 1152, "aspect_ratio": "16:9"},
        )

        conversation_workspace = self.create_workspace("应用对话画幅")
        conversation_draft = self.create_ready_prompt_draft(
            conversation_workspace,
            prompt=prompt,
            canvas_request={"width": 2048, "height": 1152, "aspect_ratio": "16:9"},
        )
        conversation = client.post(
            "/api/generations",
            json={
                **base_payload,
                "workspace_id": conversation_workspace.id,
                "prompt_draft_id": conversation_draft.id,
                "size": "2048x1152",
                "canvas_resolution": "conversation",
            },
        )
        self.assertEqual(conversation.status_code, 202, conversation.get_data(as_text=True))
        self.assertEqual(conversation.json["job"]["workflow"]["canvas_resolution"], "conversation")

    def test_legacy_canvas_request_accepts_the_nearest_valid_ratio(self):
        workspace = self.create_workspace("旧画幅建议")
        prompt = "1920×1080 横屏画面"
        draft = self.create_ready_prompt_draft(
            workspace,
            prompt=prompt,
            canvas_request={"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
        )
        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "prompt": prompt,
                "prompt_draft_id": draft.id,
                "size": "2048x1152",
                "canvas_resolution": "conversation",
            },
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(response.json["job"]["size"], "2048x1152")

    def test_legacy_workspace_size_does_not_block_other_settings(self):
        workspace = self.create_workspace("旧工作站尺寸")
        workspace.settings = {**workspace.settings, "size": "1920x1080"}
        db.session.commit()

        updated = self.services.workspaces.update(workspace, {"settings": {"prompt": "新的描述"}})
        self.assertEqual(updated.settings["size"], "auto")
        with self.assertRaisesRegex(ServiceError, "尺寸格式"):
            self.services.workspaces.update(workspace, {"settings": {"size": "1920x1080"}})

    def test_generation_api_rejects_an_invalid_stage(self):
        workspace = self.create_workspace("无效生成阶段")
        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "测试海报",
                "generation_stage": "unknown",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "生成阶段无效")

    def test_generation_service_reuses_sanitized_workflow_for_workspace_settings(self):
        workspace = self.create_workspace("工作流一致性")
        job = self.submit(workspace, workflow={"generation_stage": "unknown"})

        db.session.refresh(workspace)
        self.assertEqual(job.workflow["generation_stage"], "final")
        self.assertEqual(workspace.settings["generation_stage"], "final")

    def test_generation_api_allows_unreviewed_prompt_and_rejects_stale_claimed_review(self):
        client = self.user_client()
        workspace = self.create_workspace("审查门槛")
        payload = {
            "workspace_id": workspace.id,
            "channel_id": "test",
            "model": "model-b",
            "mode": "text2img",
            "prompt": "已审查提示词",
            "creative_direction_id": " POSTER ",
        }

        missing = client.post("/api/generations", json=payload)
        self.assertEqual(missing.status_code, 202, missing.get_data(as_text=True))
        self.assertFalse(missing.json["job"]["workflow"]["ai_reviewed"])
        self.assertEqual(missing.json["job"]["workflow"]["prompt_draft_id"], "")
        self.assertEqual(missing.json["job"]["workflow"]["creative_direction_id"], "poster")
        self.assertEqual(missing.json["job"]["workflow"]["template_label"], "用户直接提示词")

        workspace = self.create_workspace("审查过期")
        payload["workspace_id"] = workspace.id
        draft = self.create_ready_prompt_draft(workspace, prompt=payload["prompt"])
        payload["prompt_draft_id"] = draft.id.upper()
        stale = client.post(
            "/api/generations",
            json={**payload, "prompt": "被用户改过的提示词"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["code"], "prompt_review_stale")
        self.assertIn("提示词已改变", stale.json["error"])

        reviewed = client.post("/api/generations", json=payload)
        self.assertEqual(reviewed.status_code, 202, reviewed.get_data(as_text=True))
        self.assertEqual(reviewed.json["job"]["workflow"]["prompt_draft_id"], draft.id)

        reference_workspace = self.create_workspace("参考图顺序审查")
        references = self.services.workspaces.add_assets(
            reference_workspace,
            [("subject.png", png_bytes()), ("style.png", png_bytes((40, 90, 180)))],
        )
        reference_prompt = "参考图融合"
        reference_draft = self.create_ready_prompt_draft(
            reference_workspace,
            prompt=reference_prompt,
            mode="img2img",
            reference_ids=tuple(asset.id for asset in references),
        )
        stale = client.post(
            "/api/generations",
            json={
                "workspace_id": reference_workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "img2img",
                "prompt": reference_prompt,
                "prompt_draft_id": reference_draft.id,
                "reference_ids": [references[1].id, references[0].id],
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["code"], "prompt_review_stale")
        self.assertIn("参考图或顺序已改变", stale.json["error"])

    def test_bundled_channels_support_sixteen_references(self):
        config_path = Path(__file__).resolve().parents[2] / "config" / "channels.yaml"
        registry = ChannelRegistry(config_path)

        for channel_id in ("current", "lucen"):
            channel = registry.get(channel_id, require_available=False)
            self.assertEqual(channel.capabilities.max_reference_images, 16)

    def test_generation_accepts_sixteen_ordered_references(self):
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("max_reference_images: 8", "max_reference_images: 16"),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
        workspace = self.create_workspace("十六张垫图")
        assets = self.services.workspaces.add_assets(
            workspace,
            [(f"reference-{index}.png", png_bytes((index * 10, 80, 160))) for index in range(16)],
        )

        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=tuple(asset.id for asset in reversed(assets)),
        )

        self.assertEqual(
            [reference.asset_id for reference in job.references],
            [asset.id for asset in reversed(assets)],
        )

    def test_generation_rejects_seventeen_references(self):
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("max_reference_images: 8", "max_reference_images: 16"),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
        workspace = self.create_workspace("十七张垫图")
        assets = self.services.workspaces.add_assets(
            workspace,
            [(f"reference-{index}.png", png_bytes((index * 10, 80, 160))) for index in range(17)],
        )

        with self.assertRaisesRegex(ServiceError, "最多支持 16 张垫图"):
            self.submit(
                workspace,
                mode="img2img",
                reference_ids=tuple(asset.id for asset in assets),
            )

    def test_transparent_background_is_validated_persisted_and_serialized(self):
        workspace = self.create_workspace()
        with self.assertRaisesRegex(ServiceError, "透明背景仅支持 PNG 或 WebP"):
            self.submit(workspace, output_format="jpeg", transparent_background=True)

        webp_workspace = self.create_workspace("透明 WebP")
        client = self.user_client()
        draft = self.create_ready_prompt_draft(webp_workspace, prompt="极简云朵图标")
        response = client.post(
            "/api/generations",
            json={
                "workspace_id": webp_workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "极简云朵图标",
                "size": "1024x1024",
                "output_format": "webp",
                "compression": 90,
                "batch_count": 1,
                "reference_ids": [],
                "transparent_background": True,
                "prompt_draft_id": draft.id,
                "generation_stage": "final",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["job"]["transparent_background"])
        db.session.refresh(webp_workspace)
        self.assertTrue(webp_workspace.settings["transparent_background"])
        saved_job = db.session.get(GenerationJob, response.json["job"]["id"])
        self.assertTrue(saved_job.transparent_background)

    def test_custom_size_is_accepted_and_normalized(self):
        workspace = self.create_workspace()

        job = self.submit(workspace, size="1280X720")

        self.assertEqual(job.size, "1280x720")
        self.assertEqual(workspace.settings["size"], "1280x720")

    def test_auto_and_official_size_boundaries(self):
        for size in ("auto", "1024x640", "1536x512", "3840x2160"):
            with self.subTest(size=size):
                workspace = self.create_workspace(f"合法尺寸 {size}")
                self.assertEqual(self.submit(workspace, size=size).size, size)

    def test_channel_accepts_valid_custom_size_without_size_list(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, size="1536x1024")
        self.assertEqual(job.size, "1536x1024")

    def test_worker_does_not_recheck_removed_channel_size_list(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, size="1280x720")
        item = job.items[0]
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace(
                "formats: [png, jpeg, webp]",
                "sizes: [1024x1024]\n      formats: [png, jpeg, webp]",
            ),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(self.create_worker()._channel_supports_item(channel, item))

    def test_invalid_custom_size_is_rejected(self):
        workspace = self.create_workspace()
        for size in (
            "1024",
            "0x1024",
            "63x1024",
            "9000x1024",
            "1920x1080",
            "1024x624",
            "3840x2176",
            "3840x1024",
            "3856x2048",
        ):
            with self.subTest(size=size), self.assertRaisesRegex(ServiceError, "尺寸格式"):
                self.submit(workspace, size=size)

    def test_unknown_model_is_rejected(self):
        workspace = self.create_workspace()
        with self.assertRaisesRegex(ServiceError, "不支持模型"):
            self.submit(workspace, model="unknown")

    def test_multi_reference_assets_are_ordered_and_attached(self):
        workspace = self.create_workspace()
        assets = self.services.workspaces.add_assets(
            workspace,
            [("front.png", png_bytes()), ("style.png", png_bytes((40, 90, 180)))],
        )
        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=(assets[1].id, assets[0].id),
        )
        self.assertEqual(
            [reference.asset_id for reference in job.references],
            [assets[1].id, assets[0].id],
        )

    def test_generation_references_respect_runtime_total_attachment_limit(self):
        config = self.services.settings.editable_config()
        config["runtime"].update(
            {
                "max_attachment_mb": 1,
                "max_attachment_total_mb": 1,
            }
        )
        self.services.settings.save(config, self.admin.id)
        workspace = self.create_workspace()
        assets = self.services.workspaces.add_assets(
            workspace,
            [
                ("front.png", png_bytes()),
                ("style.png", png_bytes((40, 90, 180))),
            ],
        )
        for asset in assets:
            asset.byte_count = 700 * 1024
        db.session.commit()

        with self.assertRaisesRegex(ServiceError, "参考图合计不能超过 1 MiB"):
            self.submit(
                workspace,
                mode="img2img",
                reference_ids=(assets[0].id, assets[1].id),
            )

    def test_canceling_queued_batch_releases_all_reserved_balance(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=2)
        canceled = self.services.generations.cancel(job.id, user_id=self.user.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(canceled.status, "canceled")
        self.assertEqual(canceled.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertTrue(all(item.status == "canceled" for item in canceled.items))

    def test_retry_rejects_a_batch_that_still_has_active_items(self):
        workspace = self.create_workspace("仍在生成的批次")
        job = self.submit(workspace, batch_count=2)
        failed_item, queued_item = job.items
        user = db.session.get(User, self.user.id)
        failed_item.status = "failed"
        failed_item.error_code = "test_failure"
        failed_item.error_message = "测试失败"
        failed_item.completed_at = utcnow()
        job.reserved_rmb = Decimal("1.2500")
        user.reserved_rmb = Decimal("1.2500")
        db.session.commit()

        response = self.user_client().post(f"/api/generations/{job.id}/retry")

        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.json["code"], "generation_retry_conflict")
        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        self.assertEqual([item.status for item in saved.items], ["failed", "queued"])
        self.assertEqual(queued_item.status, "queued")
        self.assertEqual(saved.reserved_rmb, Decimal("1.2500"))
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("1.2500"))

    def test_retry_rejects_an_originally_queued_task(self):
        workspace = self.create_workspace("原始排队任务")
        job = self.submit(workspace)

        response = self.user_client().post(f"/api/generations/{job.id}/retry")

        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.json["code"], "generation_retry_conflict")

    def test_retry_rejects_a_disabled_account_without_changing_reservation(self):
        workspace = self.create_workspace("禁用账户重试")
        job = self.submit(workspace)
        item = job.items[0]
        item.status = "failed"
        item.error_message = "测试失败"
        item.completed_at = utcnow()
        db.session.commit()
        self.services.users.update_status(self.user.id, "disabled", self.admin.id)

        with self.assertRaisesRegex(ServiceError, "账户已被禁用") as raised:
            self.services.generations.retry(job.id, user_id=self.user.id)

        self.assertEqual(raised.exception.status_code, 403)
        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(saved.items[0].status, "failed")
        self.assertEqual(saved.reserved_rmb, Decimal("1.2500"))
        self.assertEqual(user.reserved_rmb, Decimal("1.2500"))

    def test_retry_rolls_back_when_balance_is_insufficient(self):
        workspace = self.create_workspace("余额不足重试")
        job = self.submit(workspace)
        item = job.items[0]
        item.status = "failed"
        item.error_message = "测试失败"
        item.completed_at = utcnow()
        user = db.session.get(User, self.user.id)
        user.balance_rmb = Decimal("1.0000")
        user.reserved_rmb = Decimal("0.0000")
        job.reserved_rmb = Decimal("0.0000")
        self.services.generations.refresh_job_status(job)
        db.session.commit()

        with self.assertRaisesRegex(ServiceError, "余额不足"):
            self.services.generations.retry(job.id, user_id=self.user.id)

        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(saved.items[0].status, "failed")
        self.assertEqual(saved.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(user.balance_rmb, Decimal("1.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))

    def test_refresh_job_status_preserves_partial_for_success_failure_and_cancel(self):
        workspace = self.create_workspace("混合终态")
        job = self.submit(workspace, batch_count=3)
        completed_at = utcnow()
        for item, status in zip(job.items, ("succeeded", "failed", "canceled")):
            item.status = status
            item.completed_at = completed_at
        job.workflow = {"channel_routing": {"mode": "selected"}, "_retry_pending": True}

        self.services.generations.refresh_job_status(job)

        self.assertEqual(job.status, "partial")
        self.assertNotIn("_retry_pending", job.workflow)
        self.assertEqual(job.workflow["channel_routing"]["mode"], "selected")

    def test_canceling_running_item_discards_late_provider_result(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        client = self.user_client()
        worker = self.create_worker()
        providers = BlockingProviderFactory()
        worker.providers = providers
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))

        processing = threading.Thread(target=worker._process_item, args=(job.items[0].id,))
        processing.start()
        self.assertTrue(providers.adapter.started.wait(5))
        db.session.expire_all()
        response = client.post(f"/api/generations/{job.id}/cancel")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(processing.is_alive())
        canceled = response.json["job"]
        self.assertEqual(canceled["status"], "canceled")
        self.assertFalse(canceled["can_cancel"])
        self.assertEqual(canceled["reserved_rmb"], "0.0000")
        self.assertIsNotNone(canceled["completed_at"])
        self.assertEqual(canceled["items"][0]["status"], "canceled")
        self.assertNotIn(
            job.id,
            {item["id"] for item in client.get("/api/generations/active").json["jobs"]},
        )
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0.0000"))

        replacement = client.post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "取消后立即提交",
                "generation_stage": "draft",
            },
        )
        self.assertEqual(replacement.status_code, 202, replacement.get_data(as_text=True))
        replacement_id = replacement.json["job"]["id"]
        self.assertTrue(processing.is_alive())
        self.assertEqual(
            client.post(f"/api/generations/{replacement_id}/cancel").json["job"]["status"],
            "canceled",
        )

        db.session.expire_all()
        canceled_item = db.session.get(GenerationItem, job.items[0].id)
        self.assertIsNone(canceled_item.claimed_by)
        self.assertIsNotNone(canceled_item.completed_at)
        providers.adapter.release.set()
        processing.join(10)
        self.assertFalse(processing.is_alive())

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "canceled")
        self.assertIsNone(item.output_path)
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item.id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(charge_count, 0)

    def test_cancel_requested_before_provider_call_skips_provider(self):
        workspace = self.create_workspace("调用前取消")
        job = self.submit(workspace)
        worker = self.create_worker()
        providers = FakeProviderFactory()
        worker.providers = providers
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))

        request_references = worker._request_references

        def cancel_before_request(item):
            self.services.generations.cancel(job.id, user_id=self.user.id)
            return request_references(item)

        with patch.object(worker, "_request_references", side_effect=cancel_before_request):
            worker._process_item(job.items[0].id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        self.assertEqual(item.status, "canceled")
        self.assertEqual(providers.adapter.requests, [])

    def test_canceled_request_allows_replacement_before_provider_returns(self):
        workspace = self.create_workspace("取消仍占真实槽位")
        job = self.submit(workspace)
        worker = self.create_worker()
        providers = BlockingProviderFactory()
        worker.providers = providers
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))

        processing = threading.Thread(target=worker._process_item, args=(job.items[0].id,))
        processing.start()
        try:
            self.assertTrue(providers.adapter.started.wait(5))
            self.services.generations.cancel(job.id, user_id=self.user.id)
            replacement = self.submit(workspace)
            worker._thread_pool = HoldingExecutor()

            worker._schedule_available()
            db.session.expire_all()
            attempt = db.session.scalar(
                select(GenerationAttempt).where(GenerationAttempt.item_id == job.items[0].id)
            )
            self.assertEqual(attempt.status, "running")
            self.assertEqual(
                db.session.get(GenerationItem, replacement.items[0].id).status,
                "running",
            )
        finally:
            providers.adapter.release.set()
            processing.join(10)
        self.assertFalse(processing.is_alive())
        worker._schedule_available()
        db.session.expire_all()
        self.assertEqual(attempt.status, "discarded")
        self.assertEqual(db.session.get(GenerationItem, replacement.items[0].id).status, "running")
