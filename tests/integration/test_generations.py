from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from imagegen.config.channels import ChannelRegistry
from imagegen.extensions import db
from imagegen.models import (
    GenerationItem,
    GenerationJob,
    User,
    WalletLedger,
)
from imagegen.services import ServiceError
from tests.support.platform import (
    CHANNEL_CONFIG,
    BlockingProviderFactory,
    PlatformTestCase,
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

    def test_generation_api_maps_reviewed_stages_to_quality_and_workflow(self):
        client = self.user_client()
        for stage, expected_quality in (
            ("draft", "low"),
            ("refine", "medium"),
            ("final", "high"),
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
                        "quality": "high" if expected_quality != "high" else "low",
                    },
                )

                self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
                job = response.json["job"]
                self.assertEqual(job["quality"], expected_quality)
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
        prompt = "1920×1080 横屏画面"

        unresolved_workspace = self.create_workspace("未处理画幅冲突")
        unresolved_draft = self.create_ready_prompt_draft(
            unresolved_workspace,
            prompt=prompt,
            canvas_request={"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
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
            canvas_request={"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
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
            {"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
        )

        conversation_workspace = self.create_workspace("应用对话画幅")
        conversation_draft = self.create_ready_prompt_draft(
            conversation_workspace,
            prompt=prompt,
            canvas_request={"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
        )
        conversation = client.post(
            "/api/generations",
            json={
                **base_payload,
                "workspace_id": conversation_workspace.id,
                "prompt_draft_id": conversation_draft.id,
                "size": "1920x1080",
                "canvas_resolution": "conversation",
            },
        )
        self.assertEqual(conversation.status_code, 202, conversation.get_data(as_text=True))
        self.assertEqual(conversation.json["job"]["workflow"]["canvas_resolution"], "conversation")

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
        for changed, expected_error in (
            ({"prompt": "被用户改过的提示词"}, "提示词已改变"),
            ({"mode": "img2img"}, "生成模式已改变"),
        ):
            with self.subTest(changed=changed):
                stale = client.post("/api/generations", json={**payload, **changed})
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(stale.json["code"], "prompt_review_stale")
                self.assertIn(expected_error, stale.json["error"])

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

    def test_bundled_channels_support_twenty_references(self):
        config_path = Path(__file__).resolve().parents[2] / "config" / "channels.yaml"
        registry = ChannelRegistry(config_path)

        for channel_id in ("current", "lucen"):
            channel = registry.get(channel_id, require_available=False)
            self.assertEqual(channel.capabilities.max_reference_images, 20)

    def test_generation_accepts_twenty_ordered_references(self):
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("max_reference_images: 8", "max_reference_images: 20"),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
        workspace = self.create_workspace("二十张垫图")
        assets = self.services.workspaces.add_assets(
            workspace,
            [(f"reference-{index}.png", png_bytes((index * 10, 80, 160))) for index in range(20)],
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

    def test_transparent_background_is_validated_persisted_and_serialized(self):
        workspace = self.create_workspace()
        with self.assertRaisesRegex(ServiceError, "透明背景仅支持 PNG 或 WebP"):
            self.submit(
                workspace,
                output_format="jpeg",
                transparent_background=True,
            )

        client = self.user_client()
        draft = self.create_ready_prompt_draft(workspace, prompt="极简云朵图标")
        response = client.post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "极简云朵图标",
                "size": "1024x1024",
                "output_format": "png",
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
        db.session.refresh(workspace)
        self.assertTrue(workspace.settings["transparent_background"])
        saved_job = db.session.get(GenerationJob, response.json["job"]["id"])
        self.assertTrue(saved_job.transparent_background)

    def test_custom_size_is_accepted_and_normalized(self):
        workspace = self.create_workspace()

        job = self.submit(workspace, size="1280X720")

        self.assertEqual(job.size, "1280x720")
        self.assertEqual(workspace.settings["size"], "1280x720")

    def test_invalid_custom_size_is_rejected(self):
        workspace = self.create_workspace()
        for size in ("1024", "0x1024", "63x1024", "9000x1024"):
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
