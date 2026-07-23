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
from .errors import ServiceError
from .extensions import db
from .integrations.images import (
    GenerationRequest,
    ProviderError,
    ProviderFactory,
    ReferencePayload,
)
from .integrations.matting import LucidaMattingClient
from .models import GenerationItem, GenerationJob, User, WorkerState, utcnow
from .services import RetentionService, money
from .storage import ImageStorage, StorageError
from .worker_health import worker_heartbeat_grace_seconds

LOGGER = logging.getLogger(__name__)


class GenerationWorker:
    def __init__(
        self,
        app: Flask,
        channels: ChannelRegistry,
        storage: ImageStorage,
        *,
        poll_seconds: float | None = None,
    ):
        self.app = app
        self.channels = channels
        self.storage = storage
        services = app.extensions["imagegen_services"]
        self.settings = services.settings
        self.runtime_logs = services.runtime_logs
        self.billing = services.billing
        self.generations = services.generations
        self.retention = RetentionService(storage, channels)
        self.providers = ProviderFactory()
        self.poll_seconds = poll_seconds
        hostname = socket.gethostname()[:60]
        self.worker_id = f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._stopping = threading.Event()
        self._settlement_lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._last_heartbeat = 0.0
        self._last_recovery = 0.0
        self._last_cleanup = 0.0
        self._lease_acquired = False

    def run_forever(self) -> None:
        with self.app.app_context():
            self._acquire_worker_lease()
        LOGGER.info("生成 Worker 已启动：%s", self.worker_id)
        try:
            with self.app.app_context():
                self.runtime_logs.commit_best_effort(
                    category="worker",
                    event="worker.started",
                    status="success",
                    message="生成 Worker 已启动",
                    source="worker",
                    details={"worker_id": self.worker_id},
                )
                self._recover_orphaned_items(immediate=True)
                self._last_recovery = time.monotonic()
            while not self._stopping.is_set():
                self._collect_finished()
                with self.app.app_context():
                    self.channels.reload_if_changed()
                    self._maintain_claims()
                self._schedule_available()
                self._run_periodic_cleanup()
                with self.app.app_context():
                    wait_seconds = (
                        self.poll_seconds
                        if self.poll_seconds is not None
                        else self.settings.runtime().worker_poll_milliseconds / 1000
                    )
                self._stopping.wait(wait_seconds)
        finally:
            self._stopping.set()
            self._shutdown_executor()
            LOGGER.info("生成 Worker 已停止")
            with self.app.app_context():
                self.runtime_logs.commit_best_effort(
                    category="worker",
                    event="worker.stopped",
                    status="success",
                    message="生成 Worker 已停止",
                    source="worker",
                    details={"worker_id": self.worker_id},
                )
                self._release_worker_lease()

    def _shutdown_executor(self) -> None:
        if not hasattr(self, "_thread_pool"):
            return
        self._thread_pool.shutdown(wait=False, cancel_futures=False)
        while any(not future.done() for future in self._futures.values()):
            with self.app.app_context():
                try:
                    self._heartbeat_claims()
                except Exception:
                    LOGGER.exception("Worker 退出等待期间刷新租约失败")
                    break
            time.sleep(5)
        self._thread_pool.shutdown(wait=True, cancel_futures=False)

    def _acquire_worker_lease(self) -> None:
        if db.session.get(WorkerState, 1) is None:
            raise RuntimeError("Worker 状态未初始化")
        heartbeat_seconds = self.settings.runtime().worker_heartbeat_seconds
        cutoff = (
            utcnow() - timedelta(seconds=worker_heartbeat_grace_seconds(heartbeat_seconds))
        ).replace(tzinfo=None)
        now = utcnow()
        claimed = db.session.execute(
            update(WorkerState)
            .where(
                WorkerState.id == 1,
                or_(
                    WorkerState.worker_id == self.worker_id,
                    WorkerState.worker_id.is_(None),
                    WorkerState.heartbeat_at.is_(None),
                    WorkerState.heartbeat_at < cutoff,
                ),
            )
            .values(
                worker_id=self.worker_id,
                heartbeat_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )
        if claimed.rowcount != 1:
            db.session.rollback()
            active_worker_id = db.session.scalar(
                select(WorkerState.worker_id).where(WorkerState.id == 1)
            )
            raise RuntimeError(f"已有生成 Worker 正在运行：{active_worker_id}")
        db.session.commit()
        self._lease_acquired = True

    def _release_worker_lease(self) -> None:
        if not self._lease_acquired:
            return
        db.session.execute(
            update(WorkerState)
            .where(
                WorkerState.id == 1,
                WorkerState.worker_id == self.worker_id,
            )
            .values(worker_id=None, heartbeat_at=None)
        )
        db.session.commit()
        self._lease_acquired = False

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
                LOGGER.exception("生成任务线程异常退出：%s", item_id)
                with self.app.app_context():
                    self.runtime_logs.commit_best_effort(
                        category="worker",
                        event="worker.item_crashed",
                        status="error",
                        message="生成任务线程异常退出",
                        source="worker",
                        error_code="worker_item_crashed",
                        item_id=item_id,
                        details={"worker_id": self.worker_id},
                    )

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
                    .options(
                        selectinload(GenerationItem.user),
                        selectinload(GenerationItem.job),
                    )
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
                with self._settlement_lock:
                    self._settle_canceled(item_id, started)
                return
            try:
                channel = self.channels.get(item.channel_id)
                references = self._request_references(item)
                adapter = self.providers.for_channel(channel)
                result = adapter.generate(
                    channel,
                    GenerationRequest(
                        prompt=item.prompt or item.job.prompt,
                        model=item.job.model,
                        size=item.job.size,
                        quality=item.job.quality,
                        output_format=item.job.output_format,
                        compression=item.job.compression,
                        transparent_background=item.job.transparent_background,
                        references=references,
                    ),
                )
                content = result.content
                if item.job.transparent_background:
                    content = self._apply_lucida_matting(
                        content,
                        filename=f"image_{item.id}.{item.job.output_format or 'png'}",
                    )
                with self._settlement_lock:
                    self._settle_success(item_id, content, result.request_id, started)
            except ServiceError as exc:
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code=exc.code,
                        message=str(exc),
                        upstream_status=exc.status_code,
                        upstream_request_id="",
                        started=started,
                        details={"stage": "lucida_matting"},
                    )
            except ProviderError as exc:
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code=exc.code,
                        message=str(exc),
                        upstream_status=exc.status_code,
                        upstream_request_id=exc.request_id,
                        started=started,
                        details=exc.details,
                    )
            except (StorageError, OSError) as exc:
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code="storage_error",
                        message=str(exc),
                        upstream_status=None,
                        upstream_request_id="",
                        started=started,
                        details={"exception_type": exc.__class__.__name__},
                    )
            except Exception as exc:
                LOGGER.exception("生成任务发生未预期异常：%s", item_id)
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code="internal_error",
                        message=f"内部错误：{exc.__class__.__name__}",
                        upstream_status=None,
                        upstream_request_id="",
                        started=started,
                        details={"exception_type": exc.__class__.__name__},
                    )
            finally:
                db.session.remove()

    def _apply_lucida_matting(self, content: bytes, *, filename: str) -> bytes:
        client = self.app.extensions.get("lucida_matting_client")
        if client is None:
            client = LucidaMattingClient()
        return client.remove_background(content, filename=filename)

    def _request_references(self, item: GenerationItem) -> tuple[ReferencePayload, ...]:
        return tuple(
            ReferencePayload(
                filename=reference.asset.original_name,
                content=self.storage.read_bytes(reference.asset.storage_path),
                mime_type=reference.asset.mime_type,
            )
            for reference in item.job.references
        )

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
            self.runtime_logs.record(
                category="generation",
                event="generation.provider",
                status="success",
                message="生图渠道调用成功",
                source="worker",
                user_id=item.user_id,
                user_label=user.display_name or user.username,
                workspace_id=job.workspace_id,
                workspace_label=job.workspace.name,
                job_id=job.id,
                item_id=item.id,
                provider_id=job.channel_id,
                provider_label=job.channel_label,
                model=job.model,
                upstream_request_id=request_id,
                elapsed_seconds=float(item.elapsed_seconds),
                details={
                    "output_mime_type": stored.image.mime_type,
                    "output_byte_count": stored.image.byte_count,
                    "output_width": stored.image.width,
                    "output_height": stored.image.height,
                },
            )
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
        details: dict | None = None,
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
        elapsed_seconds = round(time.monotonic() - started, 3)
        self.runtime_logs.record(
            category="generation",
            event="generation.provider",
            status="error",
            message="生图渠道调用失败",
            source="worker",
            user_id=item.user_id,
            user_label=user.display_name or user.username,
            workspace_id=job.workspace_id,
            workspace_label=job.workspace.name,
            job_id=job.id,
            item_id=item.id,
            provider_id=job.channel_id,
            provider_label=job.channel_label,
            model=job.model,
            error_code=code,
            http_status=upstream_status,
            upstream_request_id=upstream_request_id,
            elapsed_seconds=elapsed_seconds,
            details={"diagnostics": details or {}},
        )
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
        self.runtime_logs.record(
            category="generation",
            event="generation.channel_unavailable",
            status="error",
            message=item.error_message,
            source="worker",
            user_id=item.user_id,
            user_label=user.display_name or user.username,
            workspace_id=job.workspace_id,
            workspace_label=job.workspace.name,
            job_id=job.id,
            item_id=item.id,
            provider_id=job.channel_id,
            provider_label=job.channel_label,
            model=job.model,
            error_code=item.error_code,
        )
        db.session.commit()

    @staticmethod
    def _active_status(item: GenerationItem) -> bool:
        return item.status in {"running", "canceling"}

    def _owns_claim(self, item: GenerationItem) -> bool:
        return self._active_status(item) and item.claimed_by == self.worker_id

    def _maintain_claims(self) -> None:
        now = time.monotonic()
        runtime = self.settings.runtime()
        if now - self._last_heartbeat >= runtime.worker_heartbeat_seconds:
            self._heartbeat_claims()
            self._last_heartbeat = now
        if now - self._last_recovery >= runtime.worker_recovery_seconds:
            self._recover_orphaned_items(immediate=False)
            self._last_recovery = now
        db.session.remove()

    def _heartbeat_claims(self) -> None:
        now = utcnow()
        if self._lease_acquired:
            lease = db.session.execute(
                update(WorkerState)
                .where(
                    WorkerState.id == 1,
                    WorkerState.worker_id == self.worker_id,
                )
                .values(heartbeat_at=now)
            )
            if lease.rowcount != 1:
                db.session.rollback()
                self._stopping.set()
                raise RuntimeError("生成 Worker 租约已丢失")
        item_ids = tuple(self._futures)
        if item_ids:
            db.session.execute(
                update(GenerationItem)
                .where(
                    GenerationItem.id.in_(item_ids),
                    GenerationItem.claimed_by == self.worker_id,
                    GenerationItem.status.in_(["running", "canceling"]),
                )
                .values(heartbeat_at=now)
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
            LOGGER.warning("已恢复 %d 个孤立的生成任务", recovered)

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
        self.runtime_logs.record(
            category="worker",
            event="worker.recovered_item",
            status="error",
            level="warning",
            message=item.error_message,
            source="worker",
            user_id=item.user_id,
            user_label=user.display_name or user.username,
            workspace_id=job.workspace_id,
            workspace_label=job.workspace.name,
            job_id=job.id,
            item_id=item.id,
            provider_id=job.channel_id,
            provider_label=job.channel_label,
            model=job.model,
            error_code=item.error_code,
            details={"worker_id": self.worker_id, "immediate": immediate},
        )
        db.session.commit()
        return True

    def _run_periodic_cleanup(self) -> None:
        now = time.monotonic()
        result: dict[str, int] = {}
        with self.app.app_context():
            try:
                runtime = self.settings.runtime()
                interval = runtime.cleanup_interval_minutes * 60
                if now - self._last_cleanup < interval:
                    return
                result = self.retention.cleanup()
                result["runtime_logs"] = self.runtime_logs.purge(runtime.runtime_log_retention_days)
                if any(result.values()):
                    self.runtime_logs.record(
                        category="worker",
                        event="worker.retention_cleanup",
                        status="error" if result.get("errors") else "success",
                        message="定时清理部分失败" if result.get("errors") else "定时清理已完成",
                        source="worker",
                        details=result,
                    )
                    db.session.commit()
            except Exception as exc:
                db.session.rollback()
                result = {"errors": 1}
                LOGGER.exception("定时清理发生未预期异常")
                self.runtime_logs.commit_best_effort(
                    category="worker",
                    event="worker.retention_cleanup",
                    status="error",
                    message="定时清理发生未预期异常",
                    source="worker",
                    error_code="retention_cleanup_error",
                    details={"exception_type": exc.__class__.__name__},
                )
            finally:
                db.session.remove()
        self._last_cleanup = now
        if any(result.values()):
            LOGGER.info("记录清理结果：%s", result)
