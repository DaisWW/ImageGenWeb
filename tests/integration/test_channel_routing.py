from __future__ import annotations

import os
from decimal import Decimal

from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import GenerationItem, GenerationJob, RuntimeLog, User, WalletLedger
from tests.support.platform import FakeProviderFactory, HoldingExecutor, PlatformTestCase

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
    priority: 1
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
      sizes: [1024x1024]
      formats: [png, jpeg, webp]
    limits:
      max_concurrency: 2
      timeout_seconds: 600
      estimated_seconds: 120
  - id: lucen
    label: Lucen
    priority: 10
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
      sizes: [1024x1024]
      formats: [png, jpeg, webp]
    limits:
      max_concurrency: 4
      timeout_seconds: 600
      estimated_seconds: 120
"""


class TestChannelRouting(PlatformTestCase):
    def setUp(self):
        super().setUp()
        os.environ["TEST_IMAGE_KEY"] = "test-key-not-secret"
        self.channel_path.write_text(MULTI_CHANNEL_CONFIG, encoding="utf-8")
        self.app.extensions["channel_registry"].reload(force=True)

    def test_submission_routes_without_a_user_channel_and_reserves_max_price(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=4)

        self.assertEqual(job.channel_id, "current")
        self.assertEqual(job.price_per_image_rmb, Decimal("0.0900"))
        self.assertEqual(job.reserved_rmb, Decimal("0.3600"))
        self.assertEqual({item.channel_id for item in job.items}, {"__auto__"})
        self.assertEqual(job.workflow["channel_routing"]["candidate_ids"], ["current", "lucen"])

    def test_submission_ignores_a_legacy_user_channel_choice(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="lucen")

        self.assertEqual(job.channel_id, "current")
        self.assertEqual(job.workflow["channel_routing"]["candidate_ids"], ["current", "lucen"])

    def test_worker_fills_priority_channel_then_falls_back_to_lucen(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=4)
        db.session.get(User, self.user.id).generation_concurrency = 4
        db.session.commit()

        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()
        worker._schedule_available()

        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        self.assertEqual(
            [item.channel_id for item in saved.items],
            ["current", "current", "lucen", "lucen"],
        )
        self.assertEqual(
            [item.channel_label for item in saved.items], ["刀哥的", "刀哥的", "Lucen", "Lucen"]
        )
        self.assertEqual(saved.channel_id, "__mixed__")

    def test_large_single_user_batch_fills_all_channel_slots(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=8)
        db.session.get(User, self.user.id).generation_concurrency = 6
        db.session.commit()

        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()
        worker._schedule_available()

        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        self.assertEqual(sum(item.status == "running" for item in saved.items), 6)
        self.assertEqual(
            [item.channel_id for item in saved.items[:6]],
            ["current", "current", "lucen", "lucen", "lucen", "lucen"],
        )
        self.assertEqual(sum(item.status == "queued" for item in saved.items), 2)

    def test_user_concurrency_still_applies_across_all_channels(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=4)
        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()
        worker._schedule_available()

        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        self.assertEqual(sum(item.status == "running" for item in saved.items), 2)

    def test_success_log_and_charge_use_actual_routed_channel(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        worker._claim(job.items[0].id)
        worker._process_item(job.items[0].id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        log = db.session.scalar(select(RuntimeLog).where(RuntimeLog.item_id == item.id))
        self.assertEqual(item.channel_id, "current")
        self.assertEqual(item.provider_price_rmb, Decimal("0.0600"))
        self.assertEqual(item.charged_rmb, Decimal("0.0600"))
        self.assertEqual(saved_job.charged_rmb, Decimal("0.0600"))
        self.assertEqual(user.balance_rmb, Decimal("19.9400"))
        self.assertEqual(log.provider_id, "current")
        self.assertEqual(log.provider_label, "刀哥的")

    def test_success_charge_uses_the_concrete_fallback_channel_price(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        lucen = self.app.extensions["channel_registry"].get("lucen")
        self.assertTrue(worker._claim(job.items[0].id, lucen))
        worker._process_item(job.items[0].id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.channel_id, "lucen")
        self.assertEqual(item.provider_price_rmb, Decimal("0.0900"))
        self.assertEqual(item.charged_rmb, Decimal("0.0900"))
        self.assertEqual(saved_job.charged_rmb, Decimal("0.0900"))
        self.assertEqual(user.balance_rmb, Decimal("19.9100"))

    def test_free_routed_item_capture_is_idempotent(self):
        self.channel_path.write_text(
            MULTI_CHANNEL_CONFIG.replace("price_rmb: 0.0600", "price_rmb: 0.0000"),
            encoding="utf-8",
        )
        self.app.extensions["channel_registry"].reload(force=True)
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        self.assertTrue(worker._claim(job.items[0].id))
        worker._process_item(job.items[0].id)

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

    def test_admin_generation_payload_lists_each_routed_channel(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=4)
        db.session.get(User, self.user.id).generation_concurrency = 4
        db.session.commit()

        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()
        worker._schedule_available()

        db.session.remove()
        response = self.admin_client().get("/api/admin/generations")
        self.assertEqual(response.status_code, 200)
        payload = next(entry for entry in response.json["jobs"] if entry["id"] == job.id)
        self.assertEqual(
            [(entry["id"], entry["count"]) for entry in payload["channels"]],
            [("current", 2), ("lucen", 2)],
        )
        self.assertEqual(
            [item["channel"] for item in payload["items"]],
            ["刀哥的", "刀哥的", "Lucen", "Lucen"],
        )
