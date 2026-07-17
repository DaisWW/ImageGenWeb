from __future__ import annotations

import io
from decimal import Decimal

from PIL import Image
from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import (
    GenerationJob,
    User,
    Workspace,
)
from imagegen.services import ServiceError
from tests.support.platform import (
    FakeProviderFactory,
    PlatformTestCase,
    png_bytes,
)


class TestAnimations(PlatformTestCase):
    def test_animation_workspace_creation_and_parameters_are_persisted(self):
        client = self.user_client()
        response = client.post(
            "/api/workspaces",
            json={"name": "眨眼循环", "kind": "animation"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["workspace"]["kind"], "animation")
        self.assertEqual(response.json["workspace"]["settings"]["mode"], "img2img")
        self.assertTrue(response.json["workspace"]["settings"]["transparent_background"])
        self.assertEqual(response.json["workspace"]["settings"]["generation_stage"], "draft")
        self.assertEqual(response.json["workspace"]["settings"]["prompt_draft_id"], "")
        self.assertEqual(response.json["workspace"]["settings"]["animation_frame_count"], 8)
        self.assertEqual(response.json["workspace"]["settings"]["animation_fps"], 8)

        workspace = db.session.get(Workspace, response.json["workspace"]["id"])
        self.assertTrue(workspace.settings["transparent_background"])
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=(master.id,),
            frame_count=8,
            animation_fps=12,
            animation_loop=False,
            animation_format="gif",
        )

        self.assertEqual(job.kind, "animation")
        self.assertEqual(job.requested_count, 8)
        self.assertEqual(job.animation_fps, 12)
        self.assertFalse(job.animation_loop)
        self.assertEqual(job.animation_format, "gif")
        self.assertEqual(job.reserved_rmb, Decimal("10.0000"))
        self.assertEqual(workspace.settings["animation_frame_count"], 8)
        payload = client.get(f"/api/generations/{job.id}").json["job"]
        self.assertEqual(payload["kind"], "animation")
        self.assertEqual(payload["animation_duration_seconds"], 0.667)

    def test_animation_api_defaults_to_eight_frames_at_eight_fps(self):
        workspace = self.create_workspace("默认动画参数", kind="animation")
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        draft = self.create_ready_prompt_draft(
            workspace,
            prompt="角色原地奔跑循环",
            mode="img2img",
            reference_ids=(master.id,),
        )

        response = self.user_client().post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "img2img",
                "prompt": "角色原地奔跑循环",
                "reference_ids": [master.id],
                "prompt_draft_id": draft.id,
            },
        )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = response.json["job"]
        self.assertEqual(job["requested_count"], 8)
        self.assertEqual(job["animation_fps"], 8)
        self.assertEqual(job["animation_duration_seconds"], 1.0)
        self.assertEqual(job["quality"], "low")
        db.session.refresh(workspace)
        self.assertEqual(workspace.settings["animation_frame_count"], 8)
        self.assertEqual(workspace.settings["animation_fps"], 8)

    def test_animation_requires_a_user_selected_master(self):
        workspace = self.create_workspace("奔跑角色", kind="animation")
        client = self.user_client()
        payload = {
            "workspace_id": workspace.id,
            "channel_id": "test",
            "model": "model-b",
            "mode": "text2img",
            "prompt": "卡通角色侧面奔跑，透明背景",
            "reference_ids": [],
            "master_only": True,
        }

        rejected = client.post("/api/generations", json=payload)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json["error"], "请先上传或选择一张母图")
        self.assertEqual(
            db.session.scalar(
                select(func.count(GenerationJob.id)).where(
                    GenerationJob.workspace_id == workspace.id
                )
            ),
            0,
        )

        master_asset = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]

        payload.pop("master_only")
        payload.update(
            mode="img2img",
            reference_ids=[master_asset.id],
            animation_frame_count=3,
        )
        draft = self.create_ready_prompt_draft(
            workspace,
            prompt=payload["prompt"],
            mode="img2img",
            reference_ids=(master_asset.id,),
        )
        payload["prompt_draft_id"] = draft.id
        animation_response = client.post("/api/generations", json=payload)
        self.assertEqual(animation_response.status_code, 202)
        animation_payload = animation_response.json["job"]
        self.assertEqual(animation_payload["kind"], "animation")
        self.assertEqual(animation_payload["requested_count"], 3)
        self.assertEqual(
            [reference["id"] for reference in animation_payload["references"]],
            [master_asset.id],
        )

    def test_animation_rejects_multiple_master_references(self):
        workspace = self.create_workspace("双母图动画", kind="animation")
        assets = self.services.workspaces.add_assets(
            workspace,
            [
                ("master-a.png", png_bytes()),
                ("master-b.png", png_bytes((40, 90, 180))),
            ],
        )

        with self.assertRaisesRegex(ServiceError, "必须且只能选择一张母图"):
            self.submit(
                workspace,
                mode="img2img",
                reference_ids=(assets[0].id, assets[1].id),
                frame_count=3,
            )

    def test_animation_frames_run_in_order_and_export_animated_webp(self):
        workspace = self.create_workspace("挥手循环", kind="animation")
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=(master.id,),
            frame_count=3,
            animation_fps=6,
            animation_loop=True,
            animation_format="webp",
        )
        item_ids = [item.id for item in job.items]
        worker = self.create_worker()
        worker.providers = FakeProviderFactory(vary=True)
        channel = self.app.extensions["channel_registry"].get("test")

        self.assertFalse(worker._claim(item_ids[1], channel))
        for item_id in item_ids:
            self.assertTrue(worker._claim(item_id, channel))
            worker._process_item(item_id)

        db.session.expire_all()
        saved_job = db.session.get(GenerationJob, job.id)
        self.assertEqual(saved_job.status, "succeeded")
        self.assertEqual(len(worker.providers.adapter.requests), 3)
        self.assertEqual(len(worker.providers.adapter.requests[0].references), 1)
        self.assertEqual(len(worker.providers.adapter.requests[1].references), 2)
        first_request = worker.providers.adapter.requests[0]
        second_request = worker.providers.adapter.requests[1]
        self.assertEqual(second_request.references[0].filename, "master.png")
        self.assertTrue(second_request.references[1].filename.startswith("frame_001."))
        self.assertIn("frame 2 of 3", second_request.prompt)
        self.assertIn("frame duration 166.7 ms", second_request.prompt)
        self.assertIn("reference image 1 is the authoritative master", second_request.prompt)
        self.assertIn("reference image 2 is the immediately previous frame", second_request.prompt)
        self.assertIn("Never substitute color changes", second_request.prompt)
        self.assertIn("start key pose A", first_request.prompt)
        self.assertIn("first motion extreme", second_request.prompt)

        response = self.user_client().get(f"/media/animations/{job.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/webp")
        content = bytes(response.data)
        response.close()
        with Image.open(io.BytesIO(content)) as animation:
            self.assertTrue(animation.is_animated)
            self.assertEqual(animation.n_frames, 3)

    def test_animation_worker_preserves_prompt_queued_under_previous_limit(self):
        workspace = self.create_workspace("长提示词动画", kind="animation")
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        prompt = "x" * 8000
        self.assertEqual(len(prompt), 8000)
        job = self.submit(
            workspace,
            mode="img2img",
            prompt=prompt,
            reference_ids=(master.id,),
            frame_count=3,
        )
        config = self.services.settings.editable_config()
        config["runtime"]["max_prompt_characters"] = 1000
        self.services.settings.save(config, self.admin.id)
        worker = self.create_worker()

        request_prompt = worker._request_prompt(job.items[0])

        self.assertTrue(request_prompt.startswith(prompt))
        self.assertEqual(request_prompt[: len(prompt)], prompt)
        self.assertIn("Frame-by-frame animation instructions", request_prompt)

    def test_animation_failure_stops_tail_and_releases_all_reserved_balance(self):
        workspace = self.create_workspace("失败动画", kind="animation")
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=(master.id,),
            frame_count=3,
        )
        first_item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = FakeProviderFactory(fail=True)
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(first_item_id, channel))

        worker._process_item(first_item_id)

        db.session.expire_all()
        saved_job = db.session.get(GenerationJob, job.id)
        self.assertEqual(
            [item.status for item in saved_job.items], ["failed", "canceled", "canceled"]
        )
        self.assertEqual(saved_job.status, "failed")
        self.assertEqual(saved_job.items[1].error_code, "animation_dependency_failed")
        user = db.session.get(User, self.user.id)
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))

    def test_animation_retry_keeps_completed_frames_and_finishes_remaining_frames(self):
        workspace = self.create_workspace("可恢复动画", kind="animation")
        master = self.services.workspaces.add_assets(
            workspace,
            [("master.png", png_bytes())],
        )[0]
        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=(master.id,),
            frame_count=3,
        )
        worker = self.create_worker()
        worker.providers = FakeProviderFactory(vary=True)
        channel = self.app.extensions["channel_registry"].get("test")

        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)
        self.assertTrue(worker._claim(job.items[1].id, channel))
        replacement = self.create_worker()
        replacement._recover_orphaned_items(immediate=True)

        db.session.expire_all()
        failed_job = db.session.get(GenerationJob, job.id)
        first_output_path = failed_job.items[0].output_path
        self.assertEqual(
            [item.status for item in failed_job.items],
            ["succeeded", "interrupted", "canceled"],
        )
        self.assertEqual(failed_job.status, "partial")
        client = self.user_client()
        self.assertTrue(client.get(f"/api/generations/{job.id}").json["job"]["can_retry"])

        response = client.post(f"/api/generations/{job.id}/retry")

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        retried = response.json["job"]
        self.assertEqual(retried["status"], "running")
        self.assertFalse(retried["can_retry"])
        self.assertEqual(
            [item["status"] for item in retried["items"]],
            ["succeeded", "queued", "queued"],
        )
        self.assertEqual(retried["reserved_rmb"], "2.5000")
        self.assertEqual(retried["charged_rmb"], "1.2500")
        duplicate = client.post(f"/api/generations/{job.id}/retry")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json["code"], "generation_not_retryable")
        db.session.expire_all()
        saved_job = db.session.get(GenerationJob, job.id)
        self.assertEqual(saved_job.items[0].output_path, first_output_path)
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("2.5000"))

        for item in saved_job.items[1:]:
            self.assertTrue(worker._claim(item.id, channel))
            worker._process_item(item.id)

        db.session.expire_all()
        completed = db.session.get(GenerationJob, job.id)
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.items[0].output_path, first_output_path)
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0.0000"))
        animation = client.get(f"/media/animations/{job.id}")
        self.assertEqual(animation.status_code, 200)
        animation.close()
