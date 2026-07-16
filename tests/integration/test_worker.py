from __future__ import annotations

import json
import threading
from concurrent.futures import Future
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.integrations.images import (
    ProviderResult,
)
from imagegen.models import (
    GenerationItem,
    GenerationJob,
    RuntimeLog,
    User,
    WalletLedger,
    WorkerState,
    utcnow,
)
from imagegen.storage import StorageError
from tests.support.platform import (
    BlockingProviderFactory,
    FakeProviderFactory,
    HoldingExecutor,
    PlatformTestCase,
    png_bytes,
)


class TestWorker(PlatformTestCase):
    def test_worker_success_saves_image_and_charges_exactly_once(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, transparent_background=True)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)
        self.assertTrue(worker.providers.adapter.request.transparent_background)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(item.charged_rmb, Decimal("1.2500"))
        self.assertEqual(user.balance_rmb, Decimal("18.7500"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertTrue(self.app.extensions["image_storage"].read(item.output_path).is_file())
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item.id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(charge_count, 1)
        runtime_log = db.session.scalar(select(RuntimeLog).where(RuntimeLog.item_id == item.id))
        self.assertEqual(runtime_log.status, "success")
        self.assertEqual(runtime_log.provider_id, "test")
        self.assertNotIn(job.prompt, json.dumps(runtime_log.details, ensure_ascii=False))

    def test_worker_serializes_concurrent_batch_settlement(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=2)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        for item in job.items:
            self.assertTrue(worker._claim(item.id, channel))

        provider_barrier = threading.Barrier(2)

        def generate(_channel, _request):
            provider_barrier.wait(5)
            return ProviderResult(content=png_bytes(), request_id="concurrent-settlement")

        worker.providers.adapter.generate = generate
        active_settlements = 0
        peak_settlements = 0
        settlement_state_lock = threading.Lock()
        original_settle_success = worker._settle_success

        def settle_success(*args):
            nonlocal active_settlements, peak_settlements
            with settlement_state_lock:
                active_settlements += 1
                peak_settlements = max(peak_settlements, active_settlements)
            threading.Event().wait(0.1)
            try:
                original_settle_success(*args)
            finally:
                with settlement_state_lock:
                    active_settlements -= 1

        worker._settle_success = settle_success
        threads = [
            threading.Thread(target=worker._process_item, args=(item.id,)) for item in job.items
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())

        db.session.expire_all()
        saved_job = db.session.get(GenerationJob, job.id)
        user = db.session.get(User, self.user.id)
        balances = list(
            db.session.scalars(
                select(WalletLedger.balance_after_rmb)
                .where(WalletLedger.generation_item_id.in_([item.id for item in job.items]))
                .order_by(WalletLedger.id)
            )
        )
        self.assertEqual(peak_settlements, 1)
        self.assertEqual(saved_job.status, "succeeded")
        self.assertEqual(saved_job.charged_rmb, Decimal("2.5000"))
        self.assertEqual(saved_job.reserved_rmb, Decimal("0.0000"))
        self.assertTrue(all(item.status == "succeeded" for item in saved_job.items))
        self.assertEqual(user.balance_rmb, Decimal("17.5000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(balances, [Decimal("18.7500"), Decimal("17.5000")])

    def test_worker_failure_releases_reservation_without_charge(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        worker = self.create_worker()
        worker.providers = FakeProviderFactory(fail=True)
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "failed")
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        runtime_log = db.session.scalar(select(RuntimeLog).where(RuntimeLog.item_id == item.id))
        self.assertEqual(runtime_log.status, "error")
        self.assertEqual(runtime_log.error_code, "test_failure")

    def test_worker_restart_recovers_recent_claim_and_discards_late_result(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        item_id = job.items[0].id
        old_worker = self.create_worker()
        old_worker.worker_id = "worker-before-restart"
        old_worker.providers = BlockingProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(old_worker._claim(item_id, channel))

        processing = threading.Thread(target=old_worker._process_item, args=(item_id,))
        processing.start()
        self.assertTrue(old_worker.providers.adapter.started.wait(5))
        try:
            replacement = self.create_worker()
            replacement.worker_id = "worker-after-restart"
            replacement._recover_orphaned_items(immediate=True)
        finally:
            old_worker.providers.adapter.release.set()
            processing.join(10)

        self.assertFalse(processing.is_alive())
        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "interrupted")
        self.assertIsNone(item.output_path)
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item_id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(charge_count, 0)

    def test_worker_heartbeats_only_its_active_claims(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.worker_id = "heartbeat-worker"
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(item_id, channel))
        stale_heartbeat = utcnow() - timedelta(minutes=10)
        db.session.get(GenerationItem, item_id).heartbeat_at = stale_heartbeat
        db.session.commit()
        worker._futures[item_id] = Future()

        worker._heartbeat_claims()

        db.session.expire_all()
        heartbeat = db.session.get(GenerationItem, item_id).heartbeat_at
        self.assertNotEqual(heartbeat, stale_heartbeat)

    def test_worker_instances_have_unique_claim_identifiers(self):
        first = self.create_worker()
        second = self.create_worker()
        self.assertNotEqual(first.worker_id, second.worker_id)

    def test_worker_startup_always_recovers_orphaned_claims_after_leasing(self):
        worker = self.create_worker()

        with (
            patch.object(worker, "_recover_orphaned_items") as recover,
            patch.object(worker, "_maintain_claims"),
            patch.object(worker, "_schedule_available", side_effect=worker.stop),
            patch.object(worker, "_run_periodic_cleanup"),
        ):
            worker.run_forever()

        recover.assert_called_once_with(immediate=True)
        self.assertIsNone(db.session.get(WorkerState, 1).worker_id)

    def test_worker_lease_rejects_live_instance_and_allows_stale_takeover(self):
        first = self.create_worker()
        second = self.create_worker()
        first._acquire_worker_lease()

        with self.assertRaisesRegex(RuntimeError, "已有生成 Worker"):
            second._acquire_worker_lease()

        state = db.session.get(WorkerState, 1)
        state.heartbeat_at = utcnow() - timedelta(minutes=10)
        db.session.commit()
        second._acquire_worker_lease()
        self.assertEqual(db.session.get(WorkerState, 1).worker_id, second.worker_id)

        first._release_worker_lease()
        self.assertEqual(db.session.get(WorkerState, 1).worker_id, second.worker_id)
        second._release_worker_lease()
        self.assertIsNone(db.session.get(WorkerState, 1).worker_id)

    def test_idle_worker_heartbeat_drives_comprehensive_health(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/health/live").status_code, 200)
        unavailable = client.get("/health")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json["worker"], "unavailable")

        worker = self.create_worker()
        worker._acquire_worker_lease()
        state = db.session.get(WorkerState, 1)
        old_heartbeat = utcnow() - timedelta(minutes=5)
        state.heartbeat_at = old_heartbeat
        db.session.commit()

        worker._heartbeat_claims()
        db.session.expire_all()
        self.assertNotEqual(db.session.get(WorkerState, 1).heartbeat_at, old_heartbeat)
        healthy = client.get("/health")
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.json["storage"], "ready")
        self.assertEqual(healthy.json["worker"], "ready")

        db.session.get(WorkerState, 1).heartbeat_at = utcnow() - timedelta(minutes=10)
        db.session.commit()
        self.assertEqual(client.get("/health").status_code, 503)
        worker._release_worker_lease()

    def test_comprehensive_health_detects_storage_failure(self):
        state = db.session.get(WorkerState, 1)
        state.worker_id = "health-storage-worker"
        state.heartbeat_at = utcnow()
        db.session.commit()
        storage = self.app.extensions["image_storage"]

        with patch.object(storage, "healthcheck", side_effect=StorageError("read-only")):
            response = self.app.test_client().get("/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["storage"], "unavailable")

    def test_worker_periodic_recovery_skips_live_future_and_recovers_abandoned_claim(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        item_id = job.items[0].id
        worker = self.create_worker()
        worker.worker_id = "periodic-recovery-worker"
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(item_id, channel))
        db.session.get(GenerationItem, item_id).heartbeat_at = utcnow() - timedelta(minutes=30)
        db.session.commit()
        worker._futures[item_id] = Future()

        worker._recover_orphaned_items(immediate=False)
        db.session.expire_all()
        self.assertEqual(db.session.get(GenerationItem, item_id).status, "running")

        worker._futures.clear()
        worker._recover_orphaned_items(immediate=False)
        db.session.expire_all()
        self.assertEqual(db.session.get(GenerationItem, item_id).status, "interrupted")
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0.0000"))

    def test_worker_keeps_excess_images_queued_at_user_and_channel_limits(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=4)
        worker = self.create_worker()
        self.assertIs(worker.billing, self.services.billing)
        self.assertIs(worker.generations, self.services.generations)
        worker._thread_pool = HoldingExecutor()

        worker._schedule_available()
        db.session.expire_all()
        statuses = [item.status for item in db.session.get(GenerationJob, job.id).items]
        self.assertEqual(statuses.count("running"), 2)
        self.assertEqual(statuses.count("queued"), 2)

        db.session.get(User, self.user.id).generation_concurrency = 4
        db.session.commit()
        worker._schedule_available()
        db.session.expire_all()
        statuses = [item.status for item in db.session.get(GenerationJob, job.id).items]
        self.assertEqual(statuses.count("running"), 3)
        self.assertEqual(statuses.count("queued"), 1)

    def test_worker_schedules_with_its_own_application_context(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        job_id = job.id
        worker = self.create_worker()
        worker._thread_pool = HoldingExecutor()

        self.context.pop()
        try:
            worker._schedule_available()
        finally:
            self.context.push()

        db.session.expire_all()
        item = db.session.get(GenerationJob, job_id).items[0]
        self.assertEqual(item.status, "running")

    def test_queue_position_progress_and_estimated_end_are_serialized(self):
        first_workspace = self.create_workspace("第一队列")
        second_workspace = self.create_workspace("第二队列")
        first = self.submit(first_workspace)
        second = self.submit(second_workspace)
        client = self.user_client()

        queued = client.get("/api/generations?limit=10").json
        self.assertEqual(queued["queue_total"], 2)
        positions = {job["id"]: job["queue_position"] for job in queued["jobs"]}
        self.assertEqual(positions[first.id], 1)
        self.assertEqual(positions[second.id], 2)
        self.assertTrue(all(job["progress_percent"] == 0 for job in queued["jobs"]))
        self.assertTrue(all(job["estimated_end_at"] is None for job in queued["jobs"]))
        active = client.get("/api/generations/active").json["jobs"]
        active_by_id = {job["id"]: job for job in active}
        self.assertEqual(set(active_by_id), {first.id, second.id})
        self.assertEqual(active_by_id[second.id]["queue_position"], 2)
        self.assertNotIn("items", active_by_id[first.id])
        self.assertNotIn("prompt", active_by_id[first.id])

        worker = self.create_worker()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(first.items[0].id, channel))
        running = client.get(f"/api/generations/{first.id}").json["job"]
        self.assertEqual(running["status"], "running")
        self.assertGreaterEqual(running["progress_percent"], 1)
        self.assertIsNotNone(running["estimated_end_at"])
        self.assertIsNone(running["queue_position"])
        active_running = {
            job["id"]: job for job in client.get("/api/generations/active").json["jobs"]
        }[first.id]
        self.assertEqual(active_running["status"], "running")
        self.assertGreaterEqual(active_running["progress_percent"], 1)
        self.assertIsNotNone(active_running["estimated_end_at"])

    def test_generation_estimate_blends_sparse_history_with_channel_baseline(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        channel = self.app.extensions["channel_registry"].get("test")
        self.record_generation_durations([360])

        self.assertEqual(self.services.generations.estimate_seconds(job, channel), Decimal("150"))

    def test_generation_estimate_trims_extreme_runtime_samples(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        channel = self.app.extensions["channel_registry"].get("test")
        samples = [1, *([100] * 8), 1000]
        self.record_generation_durations(samples)
        self.record_generation_durations([600], model="other-model")

        self.assertEqual(self.services.generations.estimate_seconds(job, channel), Decimal("100"))
