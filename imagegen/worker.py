from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from flask import Flask
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import selectinload

from .config.channels import ChannelRegistry
from .extensions import db
from .integrations.images import (
    GenerationRequest,
    ProviderError,
    ProviderFactory,
    ReferencePayload,
)
from .models import GenerationItem, GenerationJob, User, utcnow
from .services import BillingService, GenerationService, RetentionService, money
from .storage import ImageStorage, StorageError

LOGGER = logging.getLogger(__name__)


class GenerationWorker:
    HEARTBEAT_INTERVAL_SECONDS = 15.0
    RECOVERY_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        app: Flask,
        channels: ChannelRegistry,
        storage: ImageStorage,
        *,
        poll_seconds: float = 0.5,
    ):
        self.app = app
        self.channels = channels
        self.storage = storage
        self.billing = BillingService()
        self.generations = GenerationService(channels, self.billing)
        self.retention = RetentionService(storage, channels)
        self.providers = ProviderFactory()
        self.poll_seconds = poll_seconds
        hostname = socket.gethostname()[:60]
        self.worker_id = f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._stopping = threading.Event()
        self._futures: dict[str, Future] = {}
        self._last_heartbeat = 0.0
        self._last_recovery = 0.0
        self._last_cleanup = 0.0

    def run_forever(self) -> None:
        LOGGER.info("generation worker started: %s", self.worker_id)
        with self.app.app_context():
            self._recover_orphaned_items(immediate=True)
            self._last_recovery = time.monotonic()
        try:
            while not self._stopping.is_set():
                self._collect_finished()
                with self.app.app_context():
                    self.channels.reload_if_changed()
                    self._maintain_claims()
                self._schedule_available()
                self._run_periodic_cleanup()
                self._stopping.wait(self.poll_seconds)
        finally:
            self._stopping.set()
            for future in self._futures.values():
                future.cancel()
            if hasattr(self, "_thread_pool"):
                self._thread_pool.shutdown(wait=True, cancel_futures=True)
            LOGGER.info("generation worker stopped")

    def stop(self) -> None:
        self._stopping.set()

    def _executor(self) -> ThreadPoolExecutor:
        if not hasattr(self, "_thread_pool"):
            self._thread_pool = ThreadPoolExecutor(
                max_workers=64,
                thread_name_prefix="image-generation",
            )
        return self._thread_pool

    def _collect_finished(self) -> None:
        for item_id, future in list(self._futures.items()):
            if not future.done():
                continue
            self._futures.pop(item_id, None)
            try:
                future.result()
            except Exception:
                LOGGER.exception("generation item crashed: %s", item_id)

    def _schedule_available(self) -> None:
        with self.app.app_context():
            available = self.channels.queue.global_concurrency - len(self._futures)
            if available <= 0:
                return
            active_rows = db.session.execute(
                select(
                    GenerationItem.user_id, GenerationItem.channel_id, func.count(GenerationItem.id)
                )
                .where(GenerationItem.status.in_(["running", "canceling"]))
                .group_by(GenerationItem.user_id, GenerationItem.channel_id)
            ).all()
            user_active: dict[int, int] = {}
            channel_active: dict[str, int] = {}
            for user_id, channel_id, count in active_rows:
                user_active[user_id] = user_active.get(user_id, 0) + count
                channel_active[channel_id] = channel_active.get(channel_id, 0) + count

            candidates = list(
                db.session.scalars(
                    select(GenerationItem)
                    .options(selectinload(GenerationItem.user), selectinload(GenerationItem.job))
                    .where(GenerationItem.status == "queued")
                    .order_by(GenerationItem.created_at, GenerationItem.position)
                    .limit(200)
                )
            )
            scheduled_users: set[int] = set()
            selected: list[str] = []
            for item in candidates:
                if len(selected) >= available:
                    break
                if item.user_id in scheduled_users and len(candidates) > available:
                    continue
                try:
                    channel = self.channels.get(item.channel_id)
                except ValueError:
                    self._fail_unavailable_item(item.id)
                    continue
                if channel_active.get(item.channel_id, 0) >= channel.limits.max_concurrency:
                    continue
                if user_active.get(item.user_id, 0) >= item.user.generation_concurrency:
                    continue
                if self._claim(item.id, channel):
                    selected.append(item.id)
                    scheduled_users.add(item.user_id)
                    user_active[item.user_id] = user_active.get(item.user_id, 0) + 1
                    channel_active[item.channel_id] = channel_active.get(item.channel_id, 0) + 1
            db.session.remove()

        for item_id in selected:
            self._futures[item_id] = self._executor().submit(self._process_item, item_id)

    def _claim(self, item_id: str, channel) -> bool:
        db.session.expire_all()
        job_id = db.session.scalar(
            select(GenerationItem.job_id).where(GenerationItem.id == item_id)
        )
        if job_id is None:
            db.session.rollback()
            return False

        job = db.session.scalar(
            select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
        )
        if job is None or job.cancel_requested_at:
            db.session.rollback()
            return False

        now = utcnow()
        claimed = db.session.execute(
            update(GenerationItem)
            .where(
                GenerationItem.id == item_id,
                GenerationItem.status == "queued",
                GenerationItem.cancel_requested_at.is_(None),
            )
            .values(
                status="running",
                claimed_by=self.worker_id,
                started_at=now,
                heartbeat_at=now,
            )
        )
        if claimed.rowcount != 1:
            db.session.rollback()
            return False

        item = db.session.get(GenerationItem, item_id, populate_existing=True)
        item.estimated_seconds = self.generations.estimate_seconds(job, channel)
        if job.started_at is None:
            job.started_at = now
        job.status = "running"
        db.session.commit()
        return True

    def _process_item(self, item_id: str) -> None:
        started = time.monotonic()
        with self.app.app_context():
            item = db.session.scalar(
                select(GenerationItem)
                .options(
                    selectinload(GenerationItem.job).selectinload(GenerationJob.references),
                )
                .where(GenerationItem.id == item_id)
            )
            if item is None or not self._owns_claim(item):
                return
            if item.cancel_requested_at or item.job.cancel_requested_at:
                self._settle_canceled(item_id, started)
                return
            try:
                channel = self.channels.get(item.channel_id)
                references = tuple(
                    ReferencePayload(
                        filename=reference.asset.original_name,
                        content=self.storage.read_bytes(reference.asset.storage_path),
                        mime_type=reference.asset.mime_type,
                    )
                    for reference in item.job.references
                )
                adapter = self.providers.for_channel(channel)
                result = adapter.generate(
                    channel,
                    GenerationRequest(
                        prompt=item.job.prompt,
                        model=item.job.model,
                        size=item.job.size,
                        quality=item.job.quality,
                        output_format=item.job.output_format,
                        compression=item.job.compression,
                        references=references,
                    ),
                )
                self._settle_success(item_id, result.content, result.request_id, started)
            except ProviderError as exc:
                self._settle_failure(
                    item_id,
                    code=exc.code,
                    message=str(exc),
                    upstream_status=exc.status_code,
                    upstream_request_id=exc.request_id,
                    started=started,
                )
            except (StorageError, OSError) as exc:
                self._settle_failure(
                    item_id,
                    code="storage_error",
                    message=str(exc),
                    upstream_status=None,
                    upstream_request_id="",
                    started=started,
                )
            except Exception as exc:
                LOGGER.exception("unexpected generation error: %s", item_id)
                self._settle_failure(
                    item_id,
                    code="internal_error",
                    message=f"内部错误：{exc.__class__.__name__}",
                    upstream_status=None,
                    upstream_request_id="",
                    started=started,
                )
            finally:
                db.session.remove()

    def _settle_success(
        self, item_id: str, content: bytes, request_id: str, started: float
    ) -> None:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None or not self._owns_claim(preview):
            return
        if preview.cancel_requested_at or preview.status == "canceling":
            self._settle_canceled(item_id, started)
            return
        job_preview = db.session.get(GenerationJob, preview.job_id, populate_existing=True)
        stored = self.storage.save_output(
            user_id=preview.user_id,
            workspace_id=job_preview.workspace_id,
            job_id=job_preview.id,
            item_id=preview.id,
            content=content,
        )
        try:
            db.session.expire_all()
            user = self.billing.lock_user(preview.user_id)
            item = db.session.scalar(
                select(GenerationItem)
                .where(GenerationItem.id == item_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if item is None or not self._owns_claim(item):
                db.session.rollback()
                self.storage.delete(stored.image.relative_path)
                self.storage.delete(stored.thumbnail_path)
                return
            job = db.session.scalar(
                select(GenerationJob)
                .options(selectinload(GenerationJob.items))
                .where(GenerationJob.id == item.job_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if item.cancel_requested_at or item.status == "canceling" or job.cancel_requested_at:
                self.storage.delete(stored.image.relative_path)
                self.storage.delete(stored.thumbnail_path)
                self._mark_canceled(user, job, item, started)
                db.session.commit()
                return
            item.status = "succeeded"
            item.completed_at = utcnow()
            item.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
            item.upstream_request_id = request_id[:255]
            item.output_path = stored.image.relative_path
            item.thumbnail_path = stored.thumbnail_path
            item.output_mime_type = stored.image.mime_type
            item.output_byte_count = stored.image.byte_count
            item.output_width = stored.image.width
            item.output_height = stored.image.height
            self.billing.capture(user, job, item)
            self.generations.refresh_job_status(job)
            db.session.commit()
        except Exception:
            db.session.rollback()
            self.storage.delete(stored.image.relative_path)
            self.storage.delete(stored.thumbnail_path)
            raise

    def _settle_failure(
        self,
        item_id: str,
        *,
        code: str,
        message: str,
        upstream_status: int | None,
        upstream_request_id: str,
        started: float,
    ) -> None:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None or not self._owns_claim(preview):
            return
        user = self.billing.lock_user(preview.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None or not self._owns_claim(item):
            db.session.rollback()
            return
        job = db.session.scalar(
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == item.job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item.cancel_requested_at or item.status == "canceling" or job.cancel_requested_at:
            self._mark_canceled(user, job, item, started)
        else:
            item.status = "failed"
            item.error_code = code[:80]
            item.error_message = message[:1000]
            item.upstream_status = upstream_status
            item.upstream_request_id = upstream_request_id[:255]
            item.completed_at = utcnow()
            item.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
            self.billing.release(user, job, money(job.price_per_image_rmb))
            self.generations.refresh_job_status(job)
        db.session.commit()

    def _settle_canceled(self, item_id: str, started: float) -> None:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None or not self._owns_claim(preview):
            return
        user = self.billing.lock_user(preview.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None or not self._owns_claim(item):
            db.session.rollback()
            return
        job = db.session.scalar(
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == item.job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        self._mark_canceled(user, job, item, started)
        db.session.commit()

    def _mark_canceled(
        self, user: User, job: GenerationJob, item: GenerationItem, started: float
    ) -> None:
        if item.status not in {"canceled", "succeeded", "failed", "interrupted"}:
            item.status = "canceled"
            item.cancel_requested_at = item.cancel_requested_at or utcnow()
            item.completed_at = utcnow()
            item.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
            self.billing.release(user, job, money(job.price_per_image_rmb))
        self.generations.refresh_job_status(job)

    def _cancel_claimed(self, item: GenerationItem) -> None:
        user = self.billing.lock_user(item.user_id)
        item.status = "canceled"
        item.cancel_requested_at = item.cancel_requested_at or utcnow()
        item.completed_at = utcnow()
        self.billing.release(user, item.job, money(item.job.price_per_image_rmb))
        self.generations.refresh_job_status(item.job)

    def _fail_unavailable_item(self, item_id: str) -> None:
        item = db.session.get(GenerationItem, item_id)
        if item is None or item.status != "queued":
            return
        user = self.billing.lock_user(item.user_id)
        job = db.session.scalar(
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == item.job_id)
        )
        item.status = "failed"
        item.error_code = "channel_unavailable"
        item.error_message = "渠道已禁用或 API Key 未配置"
        item.completed_at = utcnow()
        self.billing.release(user, job, money(job.price_per_image_rmb))
        self.generations.refresh_job_status(job)
        db.session.commit()

    @staticmethod
    def _active_status(item: GenerationItem) -> bool:
        return item.status in {"running", "canceling"}

    def _owns_claim(self, item: GenerationItem) -> bool:
        return self._active_status(item) and item.claimed_by == self.worker_id

    def _maintain_claims(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= self.HEARTBEAT_INTERVAL_SECONDS:
            self._heartbeat_claims()
            self._last_heartbeat = now
        if now - self._last_recovery >= self.RECOVERY_INTERVAL_SECONDS:
            self._recover_orphaned_items(immediate=False)
            self._last_recovery = now
        db.session.remove()

    def _heartbeat_claims(self) -> None:
        item_ids = tuple(self._futures)
        if not item_ids:
            return
        db.session.execute(
            update(GenerationItem)
            .where(
                GenerationItem.id.in_(item_ids),
                GenerationItem.claimed_by == self.worker_id,
                GenerationItem.status.in_(["running", "canceling"]),
            )
            .values(heartbeat_at=utcnow())
        )
        db.session.commit()

    def _recover_orphaned_items(self, *, immediate: bool) -> None:
        cutoff = utcnow() - timedelta(minutes=self.channels.queue.stale_running_minutes)
        conditions = [
            GenerationItem.status.in_(["running", "canceling"]),
        ]
        if immediate:
            conditions.append(
                or_(
                    GenerationItem.claimed_by.is_(None),
                    GenerationItem.claimed_by != self.worker_id,
                )
            )
        else:
            conditions.append(
                or_(
                    GenerationItem.heartbeat_at.is_(None),
                    GenerationItem.heartbeat_at < cutoff,
                )
            )
        item_ids = list(db.session.scalars(select(GenerationItem.id).where(*conditions)))
        recovered = 0
        for item_id in item_ids:
            if self._recover_orphaned_item(item_id, cutoff=cutoff, immediate=immediate):
                recovered += 1
        if recovered:
            LOGGER.warning("recovered %d orphaned generation items", recovered)

    def _recover_orphaned_item(self, item_id: str, *, cutoff, immediate: bool) -> bool:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None:
            return False
        user = self.billing.lock_user(preview.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        recoverable_claim = item is not None and (
            item.claimed_by != self.worker_id or item_id not in self._futures
        )
        stale_claim = True
        if item is not None and not immediate:
            comparison_cutoff = cutoff
            if item.heartbeat_at is not None and item.heartbeat_at.tzinfo is None:
                comparison_cutoff = cutoff.replace(tzinfo=None)
            stale_claim = item.heartbeat_at is None or item.heartbeat_at < comparison_cutoff
        if (
            item is None
            or not self._active_status(item)
            or not recoverable_claim
            or (not immediate and not stale_claim)
        ):
            db.session.rollback()
            return False
        job = db.session.scalar(
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == item.job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if job is None:
            db.session.rollback()
            return False
        item.status = (
            "canceled" if item.cancel_requested_at or job.cancel_requested_at else "interrupted"
        )
        item.error_code = "worker_interrupted"
        item.error_message = "Worker 中断，任务结果未知且未向用户扣费"
        item.completed_at = utcnow()
        self.billing.release(user, job, money(job.price_per_image_rmb))
        self.generations.refresh_job_status(job)
        db.session.commit()
        return True

    def _run_periodic_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 3600:
            return
        with self.app.app_context():
            result = self.retention.cleanup()
            db.session.remove()
        self._last_cleanup = now
        if result["jobs"] or result["assets"]:
            LOGGER.info("retention cleanup: %s", result)
