from __future__ import annotations

import io
import zipfile
from datetime import timedelta
from unittest.mock import patch

from PIL import Image, ImageDraw
from sqlalchemy import func, select

from imagegen.errors import ServiceError
from imagegen.extensions import db
from imagegen.models import (
    BackgroundRemovalResult,
    BackgroundRemovalRun,
    GenerationItem,
    User,
    WalletLedger,
    utcnow,
)
from tests.support.platform import FakeProviderFactory, HoldingExecutor, PlatformTestCase


def transparent_candidate(color: tuple[int, int, int], size: tuple[int, int] = (64, 48)) -> bytes:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    width, height = size
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (max(1, width // 8), max(1, height // 8), width - max(2, width // 8), height - max(2, height // 8)),
        radius=max(2, min(width, height) // 8),
        fill=(*color, 255),
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeMattingClient:
    calls: list[dict] = []
    failing_models: set[str] = set()

    def __init__(self, *, base_url, model, timeout_seconds, **_kwargs):
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds

    def remove_background(self, content: bytes, *, filename: str) -> bytes:
        self.calls.append(
            {
                "base_url": self.base_url,
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "filename": filename,
                "source": content,
            }
        )
        if self.model in self.failing_models:
            raise ServiceError(
                "测试透明化模型失败",
                code="matting_test_failure",
                status_code=502,
            )
        color = (45, 156, 96) if self.model == "lucida-v1" else (58, 116, 205)
        with Image.open(io.BytesIO(content)) as source:
            size = source.size
        return transparent_candidate(color, size)


class TestBackgroundRemoval(PlatformTestCase):
    def setUp(self):
        super().setUp()
        FakeMattingClient.calls = []
        FakeMattingClient.failing_models = set()
        self._configure_models()

    def _configure_models(self, *, lucida_concurrency=1):
        config = self.services.configuration.matting_config()
        return self.services.configuration.save_matting_models(
            {
                "revision": config["revision"],
                "models": [
                    {
                        "id": "lucida",
                        "label": "Lucida",
                        "enabled": True,
                        "base_url": "http://lucida.test",
                        "model": "lucida-v1",
                        "timeout_seconds": 30,
                        "max_concurrency": lucida_concurrency,
                    },
                    {
                        "id": "alternate",
                        "label": "备用模型",
                        "enabled": True,
                        "base_url": "http://alternate.test",
                        "model": "alternate-v1",
                        "timeout_seconds": 45,
                        "max_concurrency": 2,
                    },
                ],
            },
            self.admin.id,
        )

    def _completed_item(self, workspace_name="透明化测试"):
        workspace = self.create_workspace(workspace_name)
        job = self.submit(workspace, transparent_background=True)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)
        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        return workspace, item.job, item

    def _submit_models(self, item, *model_ids):
        response = self.user_client().post(
            f"/api/generation-items/{item.id}/background-removal",
            json={"model_ids": list(model_ids)},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json["run"]

    def _process_result(self, result_id: str):
        worker = self.create_worker()
        self.assertTrue(worker._claim_background_removal(result_id))
        db.session.expire_all()
        claimed = db.session.get(BackgroundRemovalResult, result_id)
        self.assertEqual((claimed.status, claimed.claimed_by), ("running", worker.worker_id))
        with patch.object(
            worker.background_removal.adapter_factory,
            "_lucida_client_cls",
            FakeMattingClient,
        ):
            worker._process_background_removal(result_id)

    def test_public_models_keep_lucida_first_and_submit_independent_candidates(self):
        _workspace, _job, item = self._completed_item()
        client = self.user_client()

        models = client.get("/api/background-removal-models")
        submitted = client.post(
            f"/api/generation-items/{item.id}/background-removal",
            json={"model_ids": ["lucida", "alternate"]},
        )

        self.assertEqual(models.status_code, 200)
        self.assertEqual([model["id"] for model in models.json["models"]], ["lucida", "alternate"])
        self.assertEqual(submitted.status_code, 202)
        run = submitted.json["run"]
        self.assertEqual(run["status"], "queued")
        self.assertEqual(
            [result["model_id"] for result in run["results"]],
            ["lucida", "alternate"],
        )
        self.assertEqual(len({result["id"] for result in run["results"]}), 2)

    def test_candidates_finish_independently_without_touching_original_or_billing(self):
        _workspace, job, item = self._completed_item()
        original_path = item.output_path
        original_bytes = self.app.extensions["image_storage"].read_bytes(original_path)
        user_before = db.session.get(User, self.user.id)
        balance_before = user_before.balance_rmb
        reserved_before = user_before.reserved_rmb
        ledger_before = db.session.scalar(select(func.count(WalletLedger.id)))
        run = self._submit_models(item, "lucida", "alternate")
        results = {result["model_id"]: result for result in run["results"]}
        FakeMattingClient.failing_models = {"alternate-v1"}

        self._process_result(results["lucida"]["id"])
        self._process_result(results["alternate"]["id"])

        db.session.expire_all()
        refreshed_item = db.session.get(GenerationItem, item.id)
        refreshed_run = self.services.background_removal.get_for_item(
            item.id,
            user_id=self.user.id,
        )
        by_model = {result.model_id: result for result in refreshed_run.results}
        user_after = db.session.get(User, self.user.id)
        self.assertEqual(refreshed_run.status, "partial")
        self.assertEqual(by_model["lucida"].status, "succeeded")
        self.assertEqual(by_model["alternate"].status, "failed")
        self.assertEqual(by_model["alternate"].error_code, "matting_test_failure")
        self.assertEqual(refreshed_item.status, "succeeded")
        self.assertEqual(refreshed_item.job.status, "succeeded")
        self.assertFalse(job.transparent_background)
        self.assertEqual(refreshed_item.output_path, original_path)
        self.assertEqual(
            self.app.extensions["image_storage"].read_bytes(original_path),
            original_bytes,
        )
        self.assertTrue(
            all(call["filename"].endswith(".png") for call in FakeMattingClient.calls)
        )
        self.assertNotEqual(by_model["lucida"].output_path, original_path)
        self.assertEqual(user_after.balance_rmb, balance_before)
        self.assertEqual(user_after.reserved_rmb, reserved_before)
        self.assertEqual(db.session.scalar(select(func.count(WalletLedger.id))), ledger_before)
        with Image.open(
            self.app.extensions["image_storage"].read(by_model["lucida"].output_path)
        ) as image:
            self.assertLess(image.convert("RGBA").getchannel("A").getextrema()[0], 255)

    def test_select_download_and_owner_permissions(self):
        _workspace, _job, item = self._completed_item()
        run = self._submit_models(item, "lucida", "alternate")
        for result in run["results"]:
            self._process_result(result["id"])
        client = self.user_client()
        current = client.get(f"/api/generation-items/{item.id}/background-removal").json["run"]
        first, second = current["results"]
        self.assertEqual(
            [result["status"] for result in current["results"]],
            ["succeeded", "succeeded"],
            current,
        )

        selected_first = client.post(f"/api/background-removal-results/{first['id']}/select")
        selected_second = client.post(f"/api/background-removal-results/{second['id']}/select")
        result_file = client.get(second["download_url"])
        archive_response = client.get(f"/api/background-removal-runs/{current['id']}/download")
        original_response = client.get(f"/media/outputs/{item.id}?download=1")
        result_file_data = result_file.data
        archive_data = archive_response.data
        original_data = original_response.data
        result_file.close()
        archive_response.close()
        original_response.close()

        self.assertEqual(selected_first.status_code, 200)
        self.assertEqual(selected_second.status_code, 200)
        self.assertEqual(selected_second.json["run"]["selected_result_id"], second["id"])
        self.assertEqual(
            [result["selected"] for result in selected_second.json["run"]["results"]],
            [False, True],
        )
        self.assertEqual(result_file.status_code, 200)
        self.assertEqual(result_file.mimetype, "image/png")
        self.assertTrue(result_file_data)
        self.assertEqual(archive_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
        self.assertEqual(
            original_data,
            self.app.extensions["image_storage"].read_bytes(item.output_path),
        )

        other = self.services.users.create(
            username="other-artist",
            password="StrongPass123!",
            balance_rmb="10",
            actor_user_id=self.admin.id,
        )
        image_url = second["image_url"]
        run_id = current["id"]
        other_id = other.get_id()
        self.context.pop()
        try:
            other_client = self.app.test_client()
            with other_client.session_transaction() as session:
                session["_user_id"] = other_id
                session["_fresh"] = True
            self.assertEqual(other_client.get("/api/workspaces").json["workspaces"], [])
            self.assertEqual(other_client.get(image_url).status_code, 404)
            self.assertEqual(
                other_client.get(f"/api/background-removal-runs/{run_id}/download").status_code,
                404,
            )
        finally:
            self.context.push()

    def test_admin_can_select_and_download_another_users_result(self):
        _workspace, _job, item = self._completed_item("管理员透明化")
        run = self._submit_models(item, "lucida")
        result_id = run["results"][0]["id"]
        self._process_result(result_id)

        admin_client = self.admin_client()
        selected = admin_client.post(f"/api/background-removal-results/{result_id}/select")
        downloaded = admin_client.get(
            f"/media/background-removal-results/{result_id}?download=1"
        )

        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json["run"]["selected_result_id"], result_id)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "image/png")

    def test_failed_candidate_can_retry_with_the_same_persistent_result(self):
        _workspace, _job, item = self._completed_item()
        run = self._submit_models(item, "alternate")
        result_id = run["results"][0]["id"]
        FakeMattingClient.failing_models = {"alternate-v1"}
        self._process_result(result_id)

        retried = self._submit_models(item, "alternate")

        self.assertEqual(retried["results"][0]["id"], result_id)
        self.assertEqual(retried["results"][0]["status"], "queued")
        FakeMattingClient.failing_models = set()
        self._process_result(result_id)
        completed = self.services.background_removal.get_result(
            result_id,
            user_id=self.user.id,
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertIsNotNone(completed.output_path)

    def test_worker_recovers_stale_background_removal_claims(self):
        _workspace, _job, item = self._completed_item()
        run = self._submit_models(item, "lucida")
        result_id = run["results"][0]["id"]
        old_worker = self.create_worker()
        self.assertTrue(old_worker._claim_background_removal(result_id))
        result = db.session.get(BackgroundRemovalResult, result_id)
        result.claimed_by = "dead-worker"
        result.heartbeat_at = utcnow() - timedelta(hours=1)
        db.session.commit()

        new_worker = self.create_worker()
        new_worker._recover_orphaned_items(immediate=True)

        db.session.expire_all()
        recovered = db.session.get(BackgroundRemovalResult, result_id)
        recovered_run = db.session.get(BackgroundRemovalRun, recovered.run_id)
        self.assertEqual(recovered.status, "queued")
        self.assertIsNone(recovered.claimed_by)
        self.assertEqual(recovered_run.status, "queued")

    def test_scheduler_runs_models_in_parallel_with_independent_limits(self):
        self.app.config["BACKGROUND_REMOVAL_CONCURRENCY"] = 2
        first_workspace, _first_job, first_item = self._completed_item("并行透明化一")
        _second_workspace, _second_job, second_item = self._completed_item("并行透明化二")
        self.assertNotEqual(first_workspace.id, second_item.job.workspace_id)
        self._submit_models(first_item, "lucida", "alternate")
        self._submit_models(second_item, "lucida")
        worker = self.create_worker()
        holding = HoldingExecutor()
        worker._executor = lambda: holding

        worker._schedule_background_removals()

        running = list(
            db.session.scalars(
                select(BackgroundRemovalResult).where(BackgroundRemovalResult.status == "running")
            )
        )
        queued = list(
            db.session.scalars(
                select(BackgroundRemovalResult).where(BackgroundRemovalResult.status == "queued")
            )
        )
        self.assertEqual(len(running), 2)
        self.assertEqual(len(queued), 1)
        self.assertEqual(sum(result.model_id == "lucida" for result in running), 1)
        self.assertEqual(len(worker._background_removal_futures), 2)
