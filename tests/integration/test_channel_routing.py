from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import event, func, insert, select

from imagegen.extensions import db
from imagegen.integrations.images import ProviderError, ProviderResult
from imagegen.models import (
    ChannelCircuitState,
    GenerationItem,
    GenerationJob,
    RuntimeLog,
    User,
    WalletLedger,
    utcnow,
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
  max_channel_attempts: 2
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
      failure_window_seconds: 120
      failure_threshold: 3
      circuit_breaker_seconds: 300
      half_open_max_probes: 1
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
      failure_window_seconds: 120
      failure_threshold: 3
      circuit_breaker_seconds: 300
      half_open_max_probes: 1
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

    def test_submission_honors_a_user_channel_choice(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="lucen")

        self.assertEqual(job.channel_id, "lucen")
        self.assertEqual(job.price_per_image_rmb, Decimal("0.0900"))
        self.assertEqual({item.channel_id for item in job.items}, {"lucen"})
        self.assertEqual({item.channel_label for item in job.items}, {"Lucen"})
        self.assertEqual(job.workflow["channel_routing"]["mode"], "selected")
        self.assertEqual(job.workflow["channel_routing"]["candidate_ids"], ["lucen"])
        self.assertEqual(workspace.settings["channel_id"], "lucen")

    def test_selected_channel_capacity_does_not_fall_back_to_another_channel(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="current", batch_count=4)
        db.session.get(User, self.user.id).generation_concurrency = 4
        db.session.commit()

        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()
        worker._schedule_available()

        db.session.expire_all()
        saved = db.session.get(GenerationJob, job.id)
        self.assertEqual(sum(item.status == "running" for item in saved.items), 2)
        self.assertEqual(sum(item.status == "queued" for item in saved.items), 2)
        self.assertEqual({item.channel_id for item in saved.items}, {"current"})

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

    def test_retryable_provider_error_requeues_on_next_channel_without_releasing_reservation(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {
                "current": ProviderError(
                    "刀哥暂时不可用",
                    code="upstream_error",
                    status_code=502,
                )
            }
        )

        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)

        db.session.expire_all()
        retried = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(retried.status, "queued")
        self.assertEqual(retried.channel_id, "__auto__")
        self.assertEqual(retried.attempted_channel_ids, ["current"])
        self.assertEqual(user.reserved_rmb, Decimal("0.0900"))
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))

        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)

        db.session.expire_all()
        completed = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        ledger_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item_id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        logs = list(
            db.session.scalars(
                select(RuntimeLog)
                .where(RuntimeLog.item_id == item_id)
                .order_by(RuntimeLog.created_at, RuntimeLog.id)
            )
        )
        self.assertEqual(worker.providers.calls, ["current", "lucen"])
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.channel_id, "lucen")
        self.assertEqual(completed.charged_rmb, Decimal("0.0900"))
        self.assertEqual(user.balance_rmb, Decimal("19.9100"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(ledger_count, 1)
        self.assertEqual([entry.provider_id for entry in logs], ["current", "lucen"])
        self.assertTrue(logs[0].details["will_retry"])

    def test_selected_channel_failure_does_not_retry_on_another_channel(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="current", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {
                "current": ProviderError(
                    "刀哥暂时不可用",
                    code="upstream_error",
                    status_code=502,
                )
            }
        )

        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        log = db.session.scalar(select(RuntimeLog).where(RuntimeLog.item_id == item_id))
        self.assertEqual(worker.providers.calls, ["current"])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.channel_id, "current")
        self.assertEqual(item.attempted_channel_ids, ["current"])
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertFalse(log.details["will_retry"])

    def test_non_retryable_provider_error_fails_without_opening_circuit(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="")
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {
                "current": ProviderError(
                    "请求参数无效",
                    code="upstream_error",
                    status_code=400,
                )
            }
        )

        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.attempted_channel_ids, ["current"])
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertIsNone(db.session.get(ChannelCircuitState, "current"))

    def test_all_retryable_channels_fail_once_then_release_reservation(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="")
        item_id = job.items[0].id
        failure = ProviderError("上游暂时不可用", code="upstream_error", status_code=503)
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory({"current": failure, "lucen": failure})

        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)
        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        ledger_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(WalletLedger.generation_item_id == item_id)
        )
        self.assertEqual(worker.providers.calls, ["current", "lucen"])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.attempted_channel_ids, ["current", "lucen"])
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(ledger_count, 0)

    def test_repeated_provider_errors_open_circuit_and_skip_preferred_channel(self):
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {
                "current": ProviderError(
                    "刀哥暂时不可用",
                    code="upstream_error",
                    status_code=502,
                )
            }
        )
        for index in range(3):
            job = self.submit(self.create_workspace(f"熔断测试 {index}"), channel_id="")
            self.assertTrue(worker._claim(job.items[0].id))
            worker._process_item(job.items[0].id)

        db.session.expire_all()
        state = db.session.get(ChannelCircuitState, "current")
        self.assertIsNotNone(state.open_until)
        next_job = self.submit(self.create_workspace("熔断后任务"), channel_id="")
        selected = worker._select_channel_for_item(next_job.items[0], {})
        self.assertEqual(selected.identifier, "lucen")

    def test_first_failure_recovers_from_concurrent_circuit_state_creation(self):
        job = self.submit(self.create_workspace("熔断状态并发创建"), channel_id="")
        item = db.session.get(GenerationItem, job.items[0].id)
        worker = self.create_worker()
        session = db.session()
        competing_inserted = False

        def insert_competing_state(current_session, _flush_context, _instances):
            nonlocal competing_inserted
            if competing_inserted or not any(
                isinstance(entry, ChannelCircuitState) for entry in current_session.new
            ):
                return
            competing_inserted = True
            with db.engine.begin() as connection:
                connection.execute(
                    insert(ChannelCircuitState).values(
                        channel_id="current",
                        failure_count=0,
                        updated_at=utcnow(),
                    )
                )

        event.listen(session, "before_flush", insert_competing_state)
        try:
            opened = worker._record_channel_failure(item, "current", "刀哥的")
            db.session.commit()
        finally:
            event.remove(session, "before_flush", insert_competing_state)

        state = db.session.get(ChannelCircuitState, "current")
        self.assertTrue(competing_inserted)
        self.assertFalse(opened)
        self.assertEqual(state.failure_count, 1)
        self.assertEqual(
            db.session.scalar(select(func.count(ChannelCircuitState.channel_id))),
            1,
        )

    def test_success_from_an_existing_request_does_not_close_an_open_circuit(self):
        job = self.submit(self.create_workspace("熔断前在途请求"), channel_id="")
        item_id = job.items[0].id
        worker = self.create_worker()
        self.assertTrue(worker._claim(item_id))
        db.session.add(
            ChannelCircuitState(
                channel_id="current",
                failure_count=3,
                failure_window_started_at=utcnow(),
                open_until=utcnow() + timedelta(minutes=5),
            )
        )
        db.session.commit()

        worker.providers = SelectiveProviderFactory()
        worker._process_item(item_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        state = db.session.get(ChannelCircuitState, "current")
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(state.failure_count, 3)
        self.assertIsNotNone(state.open_until)

    def test_half_open_channel_allows_one_probe_and_success_closes_circuit(self):
        worker = self.create_worker()
        db.session.add(
            ChannelCircuitState(
                channel_id="current",
                failure_count=3,
                failure_window_started_at=utcnow() - timedelta(minutes=1),
                open_until=utcnow() - timedelta(seconds=1),
            )
        )
        db.session.commit()
        first = self.submit(self.create_workspace("探测任务一"), channel_id="")
        second = self.submit(self.create_workspace("探测任务二"), channel_id="")
        current = self.app.extensions["channel_registry"].get("current")

        self.assertTrue(worker._claim(first.items[0].id, current))
        self.assertTrue(db.session.get(GenerationItem, first.items[0].id).circuit_probe)
        self.assertFalse(worker._claim(second.items[0].id, current))

        worker.providers = SelectiveProviderFactory()
        worker._process_item(first.items[0].id)

        db.session.expire_all()
        state = db.session.get(ChannelCircuitState, "current")
        self.assertEqual(state.failure_count, 0)
        self.assertIsNone(state.open_until)
        self.assertTrue(worker._claim(second.items[0].id, current))

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
