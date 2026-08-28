from __future__ import annotations

import os
from decimal import Decimal

from sqlalchemy import func, select

from imagegen.errors import ServiceError
from imagegen.extensions import db
from imagegen.integrations.images import ProviderError, ProviderResult
from imagegen.models import (
    GenerationAttempt,
    GenerationItem,
    GenerationJob,
    RuntimeLog,
    User,
    WalletLedger,
)
from tests.support.platform import (
    FakeProviderFactory,
    HoldingExecutor,
    PlatformTestCase,
    png_bytes,
)


MULTI_CHANNEL_CONFIG = """\
version: 1
queue:
  global_concurrency: 6
  max_queued_per_user: 20
  max_queued_global: 100
  history_retention_days: 30
  stale_running_minutes: 20
channels:
  - id: current
    label: 刀哥的
    enabled: true
    adapter: openai_images
    base_url: https://current.example
    api_key_env: TEST_IMAGE_KEY
    models:
      - id: model-b
        label: 模型 B
    price_rmb: 0.0600
    capabilities:
      modes: [text2img, img2img]
      max_reference_images: 8
      max_reference_image_mb: 10
      max_reference_total_mb: 40
      formats: [png, jpeg, webp]
    limits:
      max_concurrency: 2
      timeout_seconds: 600
      estimated_seconds: 120
  - id: lucen
    label: Lucen
    enabled: true
    adapter: openai_images
    base_url: https://lucen.example
    api_key_env: TEST_IMAGE_KEY
    models:
      - id: model-b
        label: 模型 B
    price_rmb: 0.0900
    capabilities:
      modes: [text2img, img2img]
      max_reference_images: 8
      max_reference_image_mb: 10
      max_reference_total_mb: 40
      formats: [png, jpeg, webp]
    limits:
      max_concurrency: 4
      timeout_seconds: 600
      estimated_seconds: 120
"""


class SelectiveProviderFactory:
    def __init__(self, failures: dict[str, ProviderError] | None = None):
        self.failures = failures or {}
        self.calls: list[str] = []

    def for_channel(self, _channel):
        return self

    def generate(self, channel, _request):
        self.calls.append(channel.identifier)
        failure = self.failures.get(channel.identifier)
        if failure is not None:
            raise failure
        return ProviderResult(content=png_bytes(), request_id=f"request-{channel.identifier}")


class RecordingSizeProviderFactory:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []

    def for_channel(self, _channel):
        return self

    def generate(self, channel, request):
        self.requests.append((channel.identifier, request.size))
        return ProviderResult(content=png_bytes(), request_id=f"request-{channel.identifier}")


class TestChannelSelection(PlatformTestCase):
    def setUp(self):
        super().setUp()
        os.environ["TEST_IMAGE_KEY"] = "test-key-not-secret"
        self.channel_path.write_text(MULTI_CHANNEL_CONFIG, encoding="utf-8")
        self.app.extensions["channel_registry"].reload(force=True)

    def test_submission_requires_an_explicit_channel(self):
        workspace = self.create_workspace()

        with self.assertRaises(ServiceError) as raised:
            self.submit(workspace, channel_id="")

        self.assertEqual(raised.exception.code, "channel_required")
        self.assertEqual(raised.exception.status_code, 422)

    def test_submission_persists_selected_channel_and_price(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="lucen", batch_count=2)

        self.assertEqual(job.channel_id, "lucen")
        self.assertEqual(job.channel_label, "Lucen")
        self.assertEqual(job.price_per_image_rmb, Decimal("0.0900"))
        self.assertEqual(job.reserved_rmb, Decimal("0.1800"))
        self.assertEqual({item.channel_id for item in job.items}, {"lucen"})
        self.assertEqual({item.channel_label for item in job.items}, {"Lucen"})
        self.assertEqual({item.provider_price_rmb for item in job.items}, {Decimal("0.0900")})
        self.assertNotIn("channel_routing", job.workflow)
        self.assertEqual(workspace.settings["channel_id"], "lucen")

    def test_selected_channels_receive_their_requested_custom_sizes(self):
        worker = self.create_worker()
        provider = RecordingSizeProviderFactory()
        worker.providers = provider

        for channel_id, size in (("current", "1536x1024"), ("lucen", "1024x1536")):
            with self.subTest(channel_id=channel_id):
                workspace = self.create_workspace(channel_id)
                job = self.submit(workspace, channel_id=channel_id, size=size)
                item = job.items[0]
                self.assertTrue(worker._claim(item.id))
                worker._process_item(item.id)
                db.session.expire_all()
                saved_item = db.session.get(GenerationItem, item.id)
                self.assertEqual((saved_item.output_width, saved_item.output_height), (64, 48))

        self.assertEqual(
            provider.requests,
            [("current", "1536x1024"), ("lucen", "1024x1536")],
        )

    def test_submission_rejects_a_full_selected_channel(self):
        first_workspace = self.create_workspace("占满刀哥")
        first = self.submit(first_workspace, channel_id="current", batch_count=2)
        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()
        worker._schedule_available()

        second_workspace = self.create_workspace("选择刀哥")
        with self.assertRaises(ServiceError) as raised:
            self.submit(second_workspace, channel_id="current")

        self.assertEqual(raised.exception.code, "channel_busy")
        self.assertIn("没有空闲槽位", str(raised.exception))
        db.session.expire_all()
        self.assertEqual(
            db.session.scalar(
                select(func.count(GenerationAttempt.id)).where(
                    GenerationAttempt.item_id.in_(item.id for item in first.items),
                    GenerationAttempt.status == "running",
                )
            ),
            2,
        )

    def test_worker_executes_only_the_selected_channel(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="lucen")
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory()

        attempt_id = worker._claim(job.items[0].id)
        self.assertIsNotNone(attempt_id)
        worker._process_item(job.items[0].id, attempt_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        attempts = list(
            db.session.scalars(
                select(GenerationAttempt).where(GenerationAttempt.item_id == item.id)
            )
        )
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(worker.providers.calls, ["lucen"])
        self.assertEqual(item.channel_id, "lucen")
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(item.charged_rmb, Decimal("0.0900"))
        self.assertEqual(saved_job.charged_rmb, Decimal("0.0900"))
        self.assertEqual(user.balance_rmb, Decimal("19.9100"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].channel_id, "lucen")
        self.assertEqual(attempts[0].status, "succeeded")

    def test_provider_error_finishes_the_selected_item_once(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="current")
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {
                "current": ProviderError("刀哥暂时不可用", code="upstream_error", status_code=502)
            }
        )

        attempt_id = worker._claim(job.items[0].id)
        self.assertIsNotNone(attempt_id)
        worker._process_item(job.items[0].id, attempt_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        attempts = list(
            db.session.scalars(
                select(GenerationAttempt).where(GenerationAttempt.item_id == item.id)
            )
        )
        user = db.session.get(User, self.user.id)
        log = db.session.scalar(select(RuntimeLog).where(RuntimeLog.item_id == item.id))
        self.assertEqual(worker.providers.calls, ["current"])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.channel_id, "current")
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "failed")
        self.assertNotIn("will_retry", log.details)
        self.assertNotIn("circuit_opened", log.details)

    def test_unknown_provider_result_is_interrupted_and_refunded(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="lucen")
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {"lucen": ProviderError("调用结果未知", code="timeout", status_code=504)}
        )

        attempt_id = worker._claim(job.items[0].id)
        self.assertIsNotNone(attempt_id)
        worker._process_item(job.items[0].id, attempt_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        attempt = db.session.scalar(
            select(GenerationAttempt).where(GenerationAttempt.item_id == item.id)
        )
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "interrupted")
        self.assertEqual(attempt.status, "unknown")
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(worker.providers.calls, ["lucen"])

    def test_public_channels_reports_current_occupancy(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="current")
        worker = self.create_worker()
        attempt_id = worker._claim(job.items[0].id)
        self.assertIsNotNone(attempt_id)

        payload = {
            channel["id"]: channel
            for channel in self.services.generations.public_channels()
        }
        self.assertEqual(payload["current"]["active_count"], 1)
        self.assertEqual(payload["current"]["available_slots"], 1)
        self.assertTrue(payload["current"]["has_capacity"])
        self.assertEqual(payload["lucen"]["active_count"], 0)
        self.assertEqual(payload["lucen"]["available_slots"], 4)

    def test_channels_api_exposes_occupancy_and_admin_hides_removed_strategy_fields(self):
        user_response = self.user_client().get("/api/channels")
        self.assertEqual(user_response.status_code, 200)
        current = next(
            channel
            for channel in user_response.json["channels"]
            if channel["id"] == "current"
        )
        self.assertEqual(current["active_count"], 0)
        self.assertEqual(current["available_slots"], 2)
        self.assertIn("has_capacity", current)

        self.context.pop()
        try:
            admin_response = self.admin_client().get("/api/admin/channels")
            self.assertEqual(admin_response.status_code, 200)
            config = admin_response.json["config"]
            self.assertNotIn("max_channel_attempts", config["queue"])
            self.assertNotIn("priority", config["channels"][0])
            self.assertNotIn("failure_threshold", config["channels"][0]["limits"])
            self.assertNotIn("circuit_breaker_seconds", config["channels"][0]["limits"])
        finally:
            self.context.push()

    def test_free_selected_item_capture_is_idempotent(self):
        self.channel_path.write_text(
            MULTI_CHANNEL_CONFIG.replace("price_rmb: 0.0600", "price_rmb: 0.0000"),
            encoding="utf-8",
        )
        self.app.extensions["channel_registry"].reload(force=True)
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="current")
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        attempt_id = worker._claim(job.items[0].id)
        self.assertIsNotNone(attempt_id)
        worker._process_item(job.items[0].id, attempt_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.charged_rmb, Decimal("0.0000"))
        self.assertEqual(saved_job.charged_rmb, Decimal("0.0000"))
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))

        worker.billing.capture(user, saved_job, item)
        self.assertEqual(
            db.session.scalar(
                select(func.count(WalletLedger.id)).where(
                    WalletLedger.generation_item_id == item.id,
                    WalletLedger.entry_type == "generation_charge",
                )
            ),
            1,
        )
