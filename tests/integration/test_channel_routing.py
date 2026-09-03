from __future__ import annotations

import os
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

from sqlalchemy import event, func, insert, select

from imagegen.extensions import db
from imagegen.integrations.images import ProviderError, ProviderResult
from imagegen.models import (
    ChannelCircuitState,
    GenerationAttempt,
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


class SequenceProviderFactory:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []
        self.requests = []

    def for_channel(self, _channel):
        return self

    def generate(self, channel, request):
        self.calls.append(channel.identifier)
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome or ProviderResult(
            content=png_bytes(), request_id=f"request-{channel.identifier}"
        )


class RecordingSizeProviderFactory:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []

    def for_channel(self, _channel):
        return self

    def generate(self, channel, request):
        self.requests.append((channel.identifier, request.size))
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

    def test_delayed_retry_finalization_does_not_touch_the_next_attempt(self):
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
        first_attempt_id = worker._claim(item_id)
        self.assertIsNotNone(first_attempt_id)

        finalization_started = threading.Event()
        allow_finalization = threading.Event()
        original_finalize_attempt = worker._finalize_attempt
        processing_errors: list[Exception] = []

        def delayed_finalize_attempt(finalized_item_id, **kwargs):
            if kwargs.get("attempt_id") == first_attempt_id:
                finalization_started.set()
                if not allow_finalization.wait(5):
                    raise TimeoutError("旧 attempt 最终化等待超时")
            return original_finalize_attempt(finalized_item_id, **kwargs)

        def process_first_attempt():
            try:
                worker._process_item(item_id, first_attempt_id)
            except Exception as exc:
                processing_errors.append(exc)

        worker._finalize_attempt = delayed_finalize_attempt
        processing = threading.Thread(target=process_first_attempt)
        second_attempt_id = None
        processing.start()
        try:
            self.assertTrue(finalization_started.wait(5))
            second_attempt_id = worker._claim(item_id)
            self.assertIsNotNone(second_attempt_id)
        finally:
            allow_finalization.set()
            processing.join(10)
            worker._finalize_attempt = original_finalize_attempt

        self.assertFalse(processing.is_alive())
        self.assertEqual(processing_errors, [])
        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        attempts = list(
            db.session.scalars(
                select(GenerationAttempt)
                .where(GenerationAttempt.item_id == item_id)
                .order_by(GenerationAttempt.attempt_number)
            )
        )
        self.assertEqual(
            [attempt.id for attempt in attempts], [first_attempt_id, second_attempt_id]
        )
        self.assertEqual([attempt.status for attempt in attempts], ["failed", "running"])
        self.assertEqual(attempts[1].claimed_by, worker.worker_id)
        self.assertEqual(item.status, "running")
        self.assertEqual(item.channel_id, "lucen")

    def test_channel_unavailable_does_not_override_cancel_that_wins_user_lock(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        original_billing = worker.billing
        billing_proxy = Mock(wraps=original_billing)
        cancellation_count = 0

        def cancel_before_worker_lock(user_id):
            nonlocal cancellation_count
            cancellation_count += 1
            self.services.generations.cancel(job.id, user_id=self.user.id)
            return original_billing.lock_user(user_id)

        billing_proxy.lock_user.side_effect = cancel_before_worker_lock
        worker.billing = billing_proxy
        try:
            worker._fail_unavailable_item(item_id)
        finally:
            worker.billing = original_billing

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        unavailable_logs = db.session.scalar(
            select(func.count(RuntimeLog.id)).where(
                RuntimeLog.item_id == item_id,
                RuntimeLog.event == "generation.channel_unavailable",
            )
        )
        self.assertEqual(cancellation_count, 1)
        self.assertEqual(item.status, "canceled")
        self.assertIsNone(item.error_code)
        self.assertEqual(saved_job.status, "canceled")
        self.assertEqual(saved_job.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(unavailable_logs, 0)
        billing_proxy.release.assert_not_called()

    def test_uncertain_provider_error_reconnects_five_times_then_stops(self):
        for code in ("timeout", "connection_error"):
            with self.subTest(code=code):
                db.session.query(ChannelCircuitState).delete()
                db.session.query(GenerationAttempt).update(
                    {GenerationAttempt.circuit_probe: False}, synchronize_session=False
                )
                db.session.commit()
                workspace = self.create_workspace(code)
                job = self.submit(workspace, channel_id="", batch_count=1)
                item_id = job.items[0].id
                worker = self.create_worker()
                worker.providers = SelectiveProviderFactory(
                    {"current": ProviderError("刀哥调用结果未知", code=code, status_code=504)}
                )

                self.assertTrue(
                    worker._claim(item_id, self.app.extensions["channel_registry"].get("current"))
                )
                attempt_id = db.session.scalar(
                    select(GenerationAttempt.id).where(GenerationAttempt.item_id == item_id)
                )
                idempotency_key = db.session.get(GenerationAttempt, attempt_id).idempotency_key
                worker._process_item(item_id)

                db.session.expire_all()
                retried = db.session.get(GenerationItem, item_id)
                user = db.session.get(User, self.user.id)
                attempt = db.session.scalar(
                    select(GenerationAttempt).where(
                        GenerationAttempt.item_id == item_id,
                        GenerationAttempt.attempt_number == 1,
                    )
                )
                self.assertEqual(retried.status, "reconnecting")
                self.assertEqual(retried.retry_count, 1)
                self.assertEqual(retried.retry_limit, 5)
                self.assertEqual(retried.attempted_channel_ids, ["current"])
                self.assertEqual(attempt.id, attempt_id)
                self.assertEqual(attempt.idempotency_key, idempotency_key)
                self.assertEqual(attempt.status, "retrying")
                self.assertEqual(user.reserved_rmb, Decimal("0.0900"))
                self.assertEqual(worker.providers.calls, ["current"])
                self.assertIsNone(worker._claim(item_id))
                self.assertTrue(
                    db.session.scalar(
                        select(RuntimeLog).where(RuntimeLog.item_id == item_id)
                    ).details["will_retry"]
                )

                for retry_count in range(2, 6):
                    db.session.expire_all()
                    retried = db.session.get(GenerationItem, item_id)
                    state = db.session.get(ChannelCircuitState, "current")
                    if state is not None and state.open_until is not None:
                        state.open_until = utcnow() - timedelta(seconds=1)
                    retried.retry_at = utcnow() - timedelta(seconds=1)
                    db.session.commit()
                    self.assertTrue(worker._claim(item_id), msg=f"retry_count={retry_count}")
                    worker._process_item(item_id)
                    db.session.expire_all()
                    retried = db.session.get(GenerationItem, item_id)
                    self.assertEqual(retried.status, "reconnecting")
                    self.assertEqual(retried.retry_count, retry_count)
                    self.assertEqual(user.reserved_rmb, Decimal("0.0900"))

                retried.retry_at = utcnow() - timedelta(seconds=1)
                state = db.session.get(ChannelCircuitState, "current")
                if state is not None and state.open_until is not None:
                    state.open_until = utcnow() - timedelta(seconds=1)
                db.session.commit()
                self.assertTrue(worker._claim(item_id))
                worker._process_item(item_id)
                db.session.expire_all()
                retried = db.session.get(GenerationItem, item_id)
                user = db.session.get(User, self.user.id)
                attempt = db.session.get(GenerationAttempt, attempt_id)
                logs = list(
                    db.session.scalars(
                        select(RuntimeLog)
                        .where(RuntimeLog.item_id == item_id)
                        .where(RuntimeLog.event == "generation.provider")
                        .order_by(RuntimeLog.created_at, RuntimeLog.id)
                    )
                )
                self.assertEqual(retried.status, "interrupted")
                self.assertEqual(retried.retry_count, 5)
                self.assertEqual(attempt.status, "unknown")
                self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
                self.assertEqual(worker.providers.calls, ["current"] * 6)
                self.assertEqual(len(logs), 6)
                self.assertTrue(all(log.details["will_retry"] for log in logs[:5]))
                self.assertFalse(logs[-1].details["will_retry"])
                self.assertTrue(logs[-1].details["result_unknown"])

    def test_timeout_reconnect_success_reuses_attempt_and_charges_once(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SequenceProviderFactory(
            [
                ProviderError("刀哥调用结果未知", code="timeout", status_code=504),
                ProviderResult(content=png_bytes(), request_id="request-after-reconnect"),
            ]
        )

        attempt_id = worker._claim(item_id)
        self.assertIsNotNone(attempt_id)
        attempt = db.session.get(GenerationAttempt, attempt_id)
        idempotency_key = attempt.idempotency_key
        worker._process_item(item_id)
        db.session.expire_all()
        self.assertEqual(db.session.get(GenerationItem, item_id).status, "reconnecting")
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0.0900"))

        self.channel_path.write_text(
            MULTI_CHANNEL_CONFIG.replace("price_rmb: 0.0600", "price_rmb: 0.1200"),
            encoding="utf-8",
        )
        self.app.extensions["channel_registry"].reload(force=True)
        item = db.session.get(GenerationItem, item_id)
        item.retry_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(worker._claim(item_id), attempt_id)
        worker._process_item(item_id)
        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        attempt = db.session.get(GenerationAttempt, attempt_id)
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item_id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(item.retry_count, 1)
        self.assertEqual(saved_job.status, "succeeded")
        self.assertEqual(saved_job.charged_rmb, Decimal("0.0600"))
        self.assertEqual(user.balance_rmb, Decimal("19.9400"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(charge_count, 1)
        self.assertEqual(attempt.status, "succeeded")
        self.assertEqual(len({request.idempotency_key for request in worker.providers.requests}), 1)
        self.assertEqual(worker.providers.requests[0].idempotency_key, idempotency_key)
        self.assertEqual(worker.providers.calls, ["current", "current"])

    def test_selected_channel_timeout_reconnects_without_switching_channel(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="current", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {"current": ProviderError("刀哥调用结果未知", code="timeout", status_code=504)}
        )

        self.assertTrue(worker._claim(item_id))
        worker._process_item(item_id)
        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        self.assertEqual(item.status, "reconnecting")
        self.assertEqual(item.channel_id, "current")
        self.assertEqual(worker.providers.calls, ["current"])
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0.0600"))

    def test_cancel_reconnecting_item_releases_reservation_and_stops_retry(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, channel_id="", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {"current": ProviderError("刀哥调用结果未知", code="timeout", status_code=504)}
        )

        attempt_id = worker._claim(item_id)
        worker._process_item(item_id)
        canceled = self.services.generations.cancel(job.id, user_id=self.user.id)
        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        attempt = db.session.get(GenerationAttempt, attempt_id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(canceled.status, "canceled")
        self.assertEqual(item.status, "canceled")
        self.assertEqual(attempt.status, "canceled")
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertIsNone(item.retry_at)
        self.assertIsNone(worker._claim(item_id))

    def test_timeout_failure_race_with_cancel_logs_without_error(self):
        workspace = self.create_workspace("超时取消竞态")
        job = self.submit(workspace, channel_id="", batch_count=1)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.providers = SelectiveProviderFactory(
            {"current": ProviderError("刀哥调用结果未知", code="timeout", status_code=504)}
        )
        original_billing = worker.billing
        billing_proxy = Mock(wraps=original_billing)
        cancellation_count = 0

        def lock_user_and_mark_cancel(user_id):
            nonlocal cancellation_count
            cancellation_count += 1
            user = original_billing.lock_user(user_id)
            item = db.session.get(GenerationItem, item_id, populate_existing=True)
            item.cancel_requested_at = utcnow()
            item.status = "canceling"
            return user

        billing_proxy.lock_user.side_effect = lock_user_and_mark_cancel
        worker.billing = billing_proxy
        try:
            self.assertTrue(worker._claim(item_id))
            worker._process_item(item_id)
        finally:
            worker.billing = original_billing

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        saved_job = db.session.get(GenerationJob, job.id)
        attempt = db.session.scalar(
            select(GenerationAttempt).where(GenerationAttempt.item_id == item_id)
        )
        user = db.session.get(User, self.user.id)
        log = db.session.scalar(
            select(RuntimeLog)
            .where(RuntimeLog.item_id == item_id)
            .where(RuntimeLog.event == "generation.provider")
        )
        self.assertEqual(cancellation_count, 1)
        self.assertEqual(item.status, "canceled")
        self.assertEqual(saved_job.status, "canceled")
        self.assertEqual(attempt.status, "canceled")
        self.assertIsNotNone(attempt.completed_at)
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertIsNotNone(log)

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

    def test_timeout_reconnect_waits_for_circuit_cooldown(self):
        workspace = self.create_workspace("重连等待熔断冷却")
        job = self.submit(workspace, channel_id="", batch_count=1)
        item_id = job.items[0].id
        timeout = ProviderError("刀哥调用结果未知", code="timeout", status_code=504)
        worker = self.create_worker()
        worker.providers = SequenceProviderFactory(
            [
                timeout,
                timeout,
                timeout,
                ProviderResult(content=png_bytes(), request_id="after-cooldown"),
            ]
        )

        for retry_count in range(1, 4):
            if retry_count == 1:
                self.assertTrue(worker._claim(item_id))
            else:
                item = db.session.get(GenerationItem, item_id)
                item.retry_at = utcnow() - timedelta(seconds=1)
                db.session.commit()
                self.assertTrue(worker._claim(item_id))
            worker._process_item(item_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        state = db.session.get(ChannelCircuitState, "current")
        self.assertEqual(item.status, "reconnecting")
        self.assertIsNotNone(state.open_until)
        item.retry_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertIsNone(worker._claim(item_id))
        self.assertEqual(worker.providers.calls, ["current"] * 3)

        state.open_until = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertTrue(worker._claim(item_id))
        self.assertTrue(db.session.get(GenerationItem, item_id).circuit_probe)
        worker._process_item(item_id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        state = db.session.get(ChannelCircuitState, "current")
        self.assertEqual(item.status, "succeeded")
        self.assertIsNone(state.open_until)
        self.assertEqual(worker.providers.calls, ["current"] * 4)

    def test_reconnecting_items_share_one_half_open_probe(self):
        worker = self.create_worker()
        timeout = ProviderError("刀哥调用结果未知", code="timeout", status_code=504)
        worker.providers = SequenceProviderFactory(
            [timeout, timeout, ProviderResult(content=png_bytes(), request_id="probe-success")]
        )
        first = self.submit(self.create_workspace("重连探测一"), channel_id="", batch_count=1)
        second = self.submit(self.create_workspace("重连探测二"), channel_id="", batch_count=1)

        self.assertTrue(worker._claim(first.items[0].id))
        worker._process_item(first.items[0].id)
        self.assertTrue(worker._claim(second.items[0].id))
        worker._process_item(second.items[0].id)

        db.session.expire_all()
        state = db.session.get(ChannelCircuitState, "current")
        self.assertEqual(state.failure_count, 2)
        state.open_until = utcnow() - timedelta(seconds=1)
        for job in (first, second):
            item = db.session.get(GenerationItem, job.items[0].id)
            item.retry_at = utcnow() - timedelta(seconds=1)
        db.session.commit()

        self.assertTrue(worker._claim(first.items[0].id))
        self.assertTrue(db.session.get(GenerationItem, first.items[0].id).circuit_probe)
        self.assertIsNone(worker._claim(second.items[0].id))
        self.assertEqual(worker.providers.calls, ["current", "current"])
        worker._process_item(first.items[0].id)

        db.session.expire_all()
        state = db.session.get(ChannelCircuitState, "current")
        self.assertIsNone(state.open_until)
        self.assertTrue(worker._claim(second.items[0].id))
        worker._process_item(second.items[0].id)
        self.assertEqual(worker.providers.calls, ["current"] * 4)

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
