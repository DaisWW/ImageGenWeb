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
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .config.channels import (
    AUTO_CHANNEL_ID,
    AUTO_CHANNEL_LABEL,
    Channel,
    ChannelRegistry,
)
from .errors import ServiceError
from .extensions import db
from .integrations.images import (
    GenerationRequest,
    ProviderError,
    ProviderFactory,
    ReferencePayload,
)
from .integrations.matting import LucidaMattingClient, image_has_baked_checkerboard
from .models import (
    BackgroundRemovalResult,
    BackgroundRemovalRun,
    ChannelCircuitState,
    GenerationAttempt,
    GenerationItem,
    GenerationJob,
    User,
    WorkerState,
    utcnow,
)
from .services import RetentionService, money
from .storage import ImageStorage, InvalidImageError, StorageError
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
        self.background_removal = services.background_removal
        self.retention = RetentionService(storage, channels)
        self.providers = ProviderFactory()
        self.poll_seconds = poll_seconds
        hostname = socket.gethostname()[:60]
        self.worker_id = f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self._stopping = threading.Event()
        self._settlement_lock = threading.Lock()
        self._dependency_lock = threading.Lock()
        self._dependency_available: dict[str, bool] = {}
        self._dependency_next_check: dict[str, float] = {}
        self._futures: dict[str, Future] = {}
        self._future_attempt_ids: dict[str, str] = {}
        self._background_removal_futures: dict[str, Future] = {}
        self._last_heartbeat = 0.0
        self._last_recovery = 0.0
        self._last_cleanup = 0.0
        self._last_loop_progress = time.monotonic()
        self._watchdog_timeout = 120
        self._watchdog_thread: threading.Thread | None = None
        self._lease_acquired = False

    def run_forever(self) -> None:
        with self.app.app_context():
            self._acquire_worker_lease()
            self._watchdog_timeout = self.settings.runtime().worker_watchdog_seconds
        self._start_watchdog()
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
                self._last_loop_progress = time.monotonic()
                self._collect_finished()
                with self.app.app_context():
                    self.channels.reload_if_changed()
                    self._maintain_claims()
                self._schedule_available()
                self._run_periodic_cleanup()
                with self.app.app_context():
                    runtime = self.settings.runtime()
                    self._watchdog_timeout = runtime.worker_watchdog_seconds
                    wait_seconds = (
                        self.poll_seconds
                        if self.poll_seconds is not None
                        else runtime.worker_poll_milliseconds / 1000
                    )
                self._last_loop_progress = time.monotonic()
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

    def _start_watchdog(self) -> None:
        if self._watchdog_thread is not None:
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="generation-worker-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self._stopping.wait(5):
            elapsed = time.monotonic() - self._last_loop_progress
            if elapsed <= self._watchdog_timeout:
                continue
            LOGGER.critical(
                "生成 Worker 主循环超过 %d 秒无进展，终止进程以触发容器重启",
                self._watchdog_timeout,
            )
            os._exit(70)

    def _shutdown_executor(self) -> None:
        if not hasattr(self, "_thread_pool"):
            return
        self._thread_pool.shutdown(wait=False, cancel_futures=False)
        while any(
            not future.done()
            for future in (*self._futures.values(), *self._background_removal_futures.values())
        ):
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
            attempt_id = self._future_attempt_ids.pop(item_id, None)
            try:
                future.result()
            except Exception:
                LOGGER.exception("生成任务线程异常退出：%s", item_id)
                with self.app.app_context():
                    self._recover_crashed_attempt(item_id, attempt_id=attempt_id)
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

        for result_id, future in list(self._background_removal_futures.items()):
            if not future.done():
                continue
            self._background_removal_futures.pop(result_id, None)
            try:
                future.result()
            except Exception:
                LOGGER.exception("透明化任务线程异常退出：%s", result_id)
                with self.app.app_context():
                    self._recover_crashed_background_removal(result_id)

    def _schedule_available(self) -> None:
        self._schedule_generation_available()
        self._schedule_background_removals()

    def _schedule_generation_available(self) -> None:
        with self.app.app_context():
            if not self._dependency_ready("storage"):
                db.session.remove()
                return
            now = utcnow()
            capacity_active = or_(
                GenerationAttempt.status == "running",
                and_(
                    GenerationAttempt.status == "unknown",
                    GenerationAttempt.capacity_expires_at > now,
                ),
            )
            active_rows = db.session.execute(
                select(
                    GenerationAttempt.user_id,
                    GenerationAttempt.channel_id,
                    GenerationAttempt.circuit_probe,
                    func.count(GenerationAttempt.id),
                )
                .where(capacity_active)
                .group_by(
                    GenerationAttempt.user_id,
                    GenerationAttempt.channel_id,
                    GenerationAttempt.circuit_probe,
                )
            ).all()
            user_active: dict[int, int] = {}
            channel_active: dict[str, int] = {}
            probe_active: dict[str, int] = {}
            database_active = 0
            for user_id, channel_id, circuit_probe, count in active_rows:
                database_active += int(count)
                user_active[user_id] = user_active.get(user_id, 0) + count
                channel_active[channel_id] = channel_active.get(channel_id, 0) + count
                if circuit_probe:
                    probe_active[channel_id] = probe_active.get(channel_id, 0) + count
            # Futures normally mirror the database rows, but counting both
            # sources protects the global cap while a crashed or restarted
            # item is waiting for stale-claim recovery.
            active = max(len(self._futures), database_active)
            available = self.channels.queue.global_concurrency - active
            if available <= 0:
                db.session.remove()
                return

            candidates = list(
                db.session.scalars(
                    select(GenerationItem)
                    .options(
                        selectinload(GenerationItem.user),
                        selectinload(GenerationItem.job).selectinload(GenerationJob.references),
                    )
                    .where(GenerationItem.status == "queued")
                    .order_by(GenerationItem.created_at, GenerationItem.position)
                    .limit(200)
                )
            )
            selected: list[tuple[str, str]] = []
            selected_ids: set[str] = set()
            unavailable_ids: set[str] = set()
            circuit_states = {
                state.channel_id: state for state in db.session.scalars(select(ChannelCircuitState))
            }
            # Give each user an initial opportunity when several users are
            # waiting, then use any remaining slots to fill their allowed
            # per-user concurrency.  This preserves queue fairness without
            # artificially limiting a single user's large batch to one item.
            passes = (True, False) if len({item.user_id for item in candidates}) > 1 else (False,)
            for first_for_user in passes:
                scheduled_users: set[int] = set()
                for item in candidates:
                    if len(selected) >= available:
                        break
                    if item.id in selected_ids:
                        continue
                    if item.id in unavailable_ids:
                        continue
                    if item.id in self._futures:
                        continue
                    if first_for_user and item.user_id in scheduled_users:
                        continue
                    channel = self._select_channel_for_item(
                        item,
                        channel_active,
                        circuit_states=circuit_states,
                        probe_active=probe_active,
                    )
                    if channel is None and not self._has_routable_channel(item):
                        self._fail_unavailable_item(item.id)
                        unavailable_ids.add(item.id)
                        continue
                    if channel is None:
                        continue
                    if user_active.get(item.user_id, 0) >= item.user.generation_concurrency:
                        continue
                    attempt_id = self._claim(item.id, channel)
                    if attempt_id is not None:
                        selected.append((item.id, attempt_id))
                        selected_ids.add(item.id)
                        scheduled_users.add(item.user_id)
                        user_active[item.user_id] = user_active.get(item.user_id, 0) + 1
                        channel_active[channel.identifier] = (
                            channel_active.get(channel.identifier, 0) + 1
                        )
                        state = circuit_states.get(channel.identifier)
                        if self._circuit_is_half_open(state):
                            probe_active[channel.identifier] = (
                                probe_active.get(channel.identifier, 0) + 1
                            )
            db.session.remove()

        for item_id, attempt_id in selected:
            self._future_attempt_ids[item_id] = attempt_id
            self._futures[item_id] = self._executor().submit(
                self._process_item, item_id, attempt_id
            )

    def _schedule_background_removals(self) -> None:
        with self.app.app_context():
            if not self._dependency_ready("storage"):
                db.session.remove()
                return

            active_rows = db.session.execute(
                select(
                    BackgroundRemovalResult.model_id,
                    func.count(BackgroundRemovalResult.id),
                )
                .where(BackgroundRemovalResult.status == "running")
                .group_by(BackgroundRemovalResult.model_id)
            ).all()
            model_active = {model_id: int(count) for model_id, count in active_rows}
            database_active = sum(model_active.values())
            active = max(len(self._background_removal_futures), database_active)
            available = int(self.app.config["BACKGROUND_REMOVAL_CONCURRENCY"]) - active
            if available <= 0:
                db.session.remove()
                return

            candidates = list(
                db.session.scalars(
                    select(BackgroundRemovalResult)
                    .where(BackgroundRemovalResult.status == "queued")
                    .order_by(BackgroundRemovalResult.created_at, BackgroundRemovalResult.id)
                    .limit(100)
                )
            )
            selected: list[str] = []
            for result in candidates:
                if len(selected) >= available:
                    break
                if result.id in self._background_removal_futures:
                    continue
                if model_active.get(result.model_id, 0) >= result.model_max_concurrency:
                    continue
                if self._claim_background_removal(result.id):
                    selected.append(result.id)
                    model_active[result.model_id] = model_active.get(result.model_id, 0) + 1
            db.session.remove()

        for result_id in selected:
            self._background_removal_futures[result_id] = self._executor().submit(
                self._process_background_removal,
                result_id,
            )

    def _claim_background_removal(self, result_id: str) -> bool:
        now = utcnow()
        claimed = db.session.execute(
            update(BackgroundRemovalResult)
            .where(
                BackgroundRemovalResult.id == result_id,
                BackgroundRemovalResult.status == "queued",
            )
            .values(
                status="running",
                claimed_by=self.worker_id,
                heartbeat_at=now,
                started_at=now,
                completed_at=None,
                elapsed_seconds=None,
                error_code=None,
                error_message=None,
            )
        )
        if claimed.rowcount != 1:
            db.session.rollback()
            return False
        result = db.session.scalar(
            select(BackgroundRemovalResult)
            .options(
                selectinload(BackgroundRemovalResult.run).selectinload(BackgroundRemovalRun.results)
            )
            .where(BackgroundRemovalResult.id == result_id)
            .execution_options(populate_existing=True)
        )
        if result is None:
            db.session.rollback()
            return False
        self.background_removal.refresh_run_status(result.run)
        db.session.commit()
        return True

    def _process_background_removal(self, result_id: str) -> None:
        started = time.monotonic()
        with self.app.app_context():
            result = db.session.scalar(
                select(BackgroundRemovalResult)
                .options(
                    selectinload(BackgroundRemovalResult.run)
                    .selectinload(BackgroundRemovalRun.source_item)
                    .selectinload(GenerationItem.job)
                )
                .where(BackgroundRemovalResult.id == result_id)
                .execution_options(populate_existing=True)
            )
            if result is None or not self._owns_background_removal_claim(result):
                db.session.remove()
                return
            item = result.run.source_item
            if item.status != "succeeded" or not item.output_path:
                self._fail_background_removal(
                    result_id,
                    code="source_image_unavailable",
                    message="原始生成图片不存在或尚未完成",
                    started=started,
                )
                db.session.remove()
                return

            try:
                source = self.storage.read_bytes(item.output_path)
                client = LucidaMattingClient(
                    base_url=result.model_base_url,
                    model=result.upstream_model,
                    timeout_seconds=float(result.model_timeout_seconds),
                )
                content = client.remove_background(
                    source,
                    filename=f"image_{item.id}",
                )
                if image_has_baked_checkerboard(content):
                    raise ServiceError(
                        "透明化结果疑似仍包含棋盘格像素，请尝试其他模型",
                        code="matting_checkerboard_result",
                        status_code=502,
                    )

                db.session.expire_all()
                latest = db.session.get(
                    BackgroundRemovalResult,
                    result_id,
                    populate_existing=True,
                )
                if latest is None or not self._owns_background_removal_claim(latest):
                    return
                stored = self.storage.save_background_removal(
                    user_id=item.user_id,
                    workspace_id=item.job.workspace_id,
                    job_id=item.job_id,
                    result_id=result_id,
                    content=content,
                )
                try:
                    if not self._finish_background_removal(result_id, stored, started):
                        self.storage.delete(stored.thumbnail_path)
                        self.storage.delete(stored.image.relative_path)
                        return
                except Exception:
                    db.session.rollback()
                    self.storage.delete(stored.thumbnail_path)
                    self.storage.delete(stored.image.relative_path)
                    raise
                LOGGER.info(
                    "透明化任务完成：%s（%s）",
                    result_id,
                    result.model_label,
                )
            except ServiceError as exc:
                self._fail_background_removal(
                    result_id,
                    code=exc.code,
                    message=str(exc),
                    started=started,
                )
            except (StorageError, OSError) as exc:
                self._pause_dependency("storage", exc)
                self._fail_background_removal(
                    result_id,
                    code="storage_error",
                    message=str(exc),
                    started=started,
                )
            except Exception as exc:
                LOGGER.exception("透明化任务发生未预期异常：%s", result_id)
                self._fail_background_removal(
                    result_id,
                    code="internal_error",
                    message=f"内部错误：{exc.__class__.__name__}",
                    started=started,
                )
            finally:
                db.session.remove()

    def _finish_background_removal(self, result_id: str, stored, started: float) -> bool:
        result = db.session.scalar(
            select(BackgroundRemovalResult)
            .options(
                selectinload(BackgroundRemovalResult.run).selectinload(BackgroundRemovalRun.results)
            )
            .where(BackgroundRemovalResult.id == result_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if result is None or not self._owns_background_removal_claim(result):
            db.session.rollback()
            return False
        result.status = "succeeded"
        result.claimed_by = None
        result.heartbeat_at = None
        result.completed_at = utcnow()
        result.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
        result.output_path = stored.image.relative_path
        result.thumbnail_path = stored.thumbnail_path
        result.output_mime_type = stored.image.mime_type
        result.output_byte_count = stored.image.byte_count
        result.output_width = stored.image.width
        result.output_height = stored.image.height
        result.error_code = None
        result.error_message = None
        self.background_removal.refresh_run_status(result.run)
        db.session.commit()
        return True

    def _fail_background_removal(
        self,
        result_id: str,
        *,
        code: str,
        message: str,
        started: float,
    ) -> None:
        db.session.expire_all()
        result = db.session.scalar(
            select(BackgroundRemovalResult)
            .options(
                selectinload(BackgroundRemovalResult.run).selectinload(BackgroundRemovalRun.results)
            )
            .where(BackgroundRemovalResult.id == result_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if result is None or not self._owns_background_removal_claim(result):
            db.session.rollback()
            return
        result.status = "failed"
        result.selected = False
        result.claimed_by = None
        result.heartbeat_at = None
        result.completed_at = utcnow()
        result.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
        result.error_code = code[:80]
        result.error_message = message[:1000]
        self.background_removal.refresh_run_status(result.run)
        db.session.commit()
        LOGGER.warning("透明化任务失败：%s（%s）：%s", result_id, code, message)

    def _owns_background_removal_claim(self, result: BackgroundRemovalResult) -> bool:
        return result.status == "running" and result.claimed_by == self.worker_id

    def _dependency_ready(self, name: str) -> bool:
        now = time.monotonic()
        with self._dependency_lock:
            if now < self._dependency_next_check.get(name, 0.0):
                return self._dependency_available.get(name, False)
            runtime = self.settings.runtime()
            try:
                if name == "storage":
                    self.storage.healthcheck(
                        minimum_free_bytes=runtime.storage_min_free_mb * 1024 * 1024
                    )
                else:
                    raise ValueError(f"未知 Worker 依赖：{name}")
            except Exception as exc:
                self._set_dependency_state(name, False, runtime.dependency_retry_seconds, exc)
                return False
            self._set_dependency_state(name, True, 5, None)
            return True

    def _pause_dependency(self, name: str, error: Exception) -> None:
        runtime = self.settings.runtime()
        with self._dependency_lock:
            self._set_dependency_state(name, False, runtime.dependency_retry_seconds, error)

    def _set_dependency_state(
        self,
        name: str,
        available: bool,
        retry_seconds: int,
        error: Exception | None,
    ) -> None:
        previous = self._dependency_available.get(name)
        self._dependency_available[name] = available
        self._dependency_next_check[name] = time.monotonic() + retry_seconds
        if previous is available or (previous is None and available):
            return
        label = "图片存储"
        self.runtime_logs.commit_best_effort(
            category="worker",
            event="worker.dependency_recovered" if available else "worker.dependency_paused",
            status="success" if available else "error",
            level="info" if available else "warning",
            message=f"{label}已恢复调度" if available else f"{label}不可用，已暂停相关调度",
            source="worker",
            error_code="" if available else f"{name}_unavailable",
            details={
                "dependency": name,
                "retry_seconds": retry_seconds,
                "error": "" if error is None else str(error)[:500],
            },
        )

    def _claim(self, item_id: str, channel: Channel | None = None) -> str | None:
        db.session.expire_all()
        item_preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if item_preview is None or item_preview.status != "queued":
            db.session.rollback()
            return None
        if (
            db.session.scalar(
                select(GenerationAttempt.id).where(
                    GenerationAttempt.item_id == item_id,
                    GenerationAttempt.status == "running",
                )
            )
            is not None
        ):
            db.session.rollback()
            return None
        job = db.session.scalar(
            select(GenerationJob).where(GenerationJob.id == item_preview.job_id).with_for_update()
        )
        if job is None or job.cancel_requested_at:
            db.session.rollback()
            return None
        if channel is None:
            channel = self._select_channel_for_item(item_preview, {})
        if channel is None:
            db.session.rollback()
            return None
        if channel.price_rmb > job.price_per_image_rmb:
            db.session.rollback()
            return None
        if not channel.configured or not self._channel_supports_item(channel, item_preview):
            db.session.rollback()
            return None
        if (
            item_preview.channel_id not in {"", AUTO_CHANNEL_ID}
            and item_preview.channel_id != channel.identifier
        ):
            db.session.rollback()
            return None

        circuit_probe = self._reserve_circuit_probe(item_id, channel)
        if circuit_probe is None:
            db.session.rollback()
            return None

        now = utcnow()
        attempt_number = (
            int(
                db.session.scalar(
                    select(func.coalesce(func.max(GenerationAttempt.attempt_number), 0)).where(
                        GenerationAttempt.item_id == item_id
                    )
                )
                or 0
            )
            + 1
        )
        attempt = GenerationAttempt(
            id=uuid.uuid4().hex,
            item_id=item_id,
            user_id=item_preview.user_id,
            channel_id=channel.identifier,
            channel_label=channel.label,
            attempt_number=attempt_number,
            idempotency_key=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"imagegen:{item_id}:{attempt_number}",
            ).hex,
            status="running",
            circuit_probe=circuit_probe,
            claimed_by=self.worker_id,
            heartbeat_at=now,
            started_at=now,
            capacity_expires_at=now + timedelta(seconds=channel.limits.timeout_seconds + 30),
        )
        db.session.add(attempt)
        claimed = db.session.execute(
            update(GenerationItem)
            .where(
                GenerationItem.id == item_id,
                GenerationItem.status == "queued",
                GenerationItem.cancel_requested_at.is_(None),
            )
            .values(
                status="running",
                channel_id=channel.identifier,
                channel_label=channel.label,
                provider_price_rmb=money(channel.price_rmb),
                circuit_probe=circuit_probe,
                claimed_by=self.worker_id,
                started_at=now,
                heartbeat_at=now,
            )
        )
        if claimed.rowcount != 1:
            db.session.rollback()
            return None

        item = db.session.get(GenerationItem, item_id, populate_existing=True)
        item.estimated_seconds = self.generations.estimate_seconds(job, channel)
        if job.started_at is None:
            job.started_at = now
        job.status = "running"
        self.generations.refresh_job_channel_summary(job)
        db.session.commit()
        return attempt.id

    def _select_channel_for_item(
        self,
        item: GenerationItem,
        channel_active: dict[str, int],
        *,
        circuit_states: dict[str, ChannelCircuitState] | None = None,
        probe_active: dict[str, int] | None = None,
    ) -> Channel | None:
        """Pick the first capable channel with an open slot.

        The registry is priority ordered, so iterating it implements the
        administrator's lower-number-first policy while still filling the
        next provider when a higher-priority channel is saturated.
        """

        if circuit_states is None:
            circuit_states = {
                state.channel_id: state for state in db.session.scalars(select(ChannelCircuitState))
            }
        if probe_active is None:
            probe_active = dict(
                db.session.execute(
                    select(GenerationAttempt.channel_id, func.count(GenerationAttempt.id))
                    .where(
                        GenerationAttempt.circuit_probe.is_(True),
                        or_(
                            GenerationAttempt.status == "running",
                            and_(
                                GenerationAttempt.status == "unknown",
                                GenerationAttempt.capacity_expires_at > utcnow(),
                            ),
                        ),
                    )
                    .group_by(GenerationAttempt.channel_id)
                ).all()
            )
        for channel in self._routable_channels(item):
            if channel_active.get(channel.identifier, 0) >= channel.limits.max_concurrency:
                continue
            state = circuit_states.get(channel.identifier)
            if self._circuit_is_open(state):
                continue
            if (
                self._circuit_is_half_open(state)
                and probe_active.get(channel.identifier, 0) >= channel.limits.half_open_max_probes
            ):
                continue
            return channel
        return None

    def _has_routable_channel(self, item: GenerationItem) -> bool:
        """Return whether the item has any capable configured provider.

        This intentionally ignores capacity; callers use it to distinguish a
        temporarily full queue from a permanently unavailable configuration.
        """

        return bool(self._routable_channels(item))

    def _routable_channels(self, item: GenerationItem) -> tuple[Channel, ...]:
        if item.channel_id not in {"", AUTO_CHANNEL_ID}:
            try:
                channels = (self.channels.get(item.channel_id, require_available=False),)
            except ValueError:
                return ()
        else:
            channels = tuple(self.channels.list(include_disabled=False))
        attempted = set(item.attempted_channel_ids or [])
        return tuple(
            channel
            for channel in channels
            if channel.configured
            and channel.identifier not in attempted
            and channel.price_rmb <= item.job.price_per_image_rmb
            and self._channel_supports_item(channel, item)
        )

    def _reserve_circuit_probe(self, item_id: str, channel: Channel) -> bool | None:
        state = db.session.scalar(
            select(ChannelCircuitState)
            .where(ChannelCircuitState.channel_id == channel.identifier)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if state is None or state.open_until is None:
            return False
        if self._circuit_is_open(state):
            return None
        active_probes = db.session.scalar(
            select(func.count(GenerationAttempt.id)).where(
                GenerationAttempt.item_id != item_id,
                GenerationAttempt.channel_id == channel.identifier,
                GenerationAttempt.circuit_probe.is_(True),
                or_(
                    GenerationAttempt.status == "running",
                    and_(
                        GenerationAttempt.status == "unknown",
                        GenerationAttempt.capacity_expires_at > utcnow(),
                    ),
                ),
            )
        )
        if int(active_probes or 0) >= channel.limits.half_open_max_probes:
            return None
        return True

    @classmethod
    def _circuit_is_open(cls, state: ChannelCircuitState | None) -> bool:
        return bool(state and state.open_until and cls._time_is_after(state.open_until, utcnow()))

    @classmethod
    def _circuit_is_half_open(cls, state: ChannelCircuitState | None) -> bool:
        return bool(
            state and state.open_until and not cls._time_is_after(state.open_until, utcnow())
        )

    @staticmethod
    def _time_is_after(value, reference) -> bool:
        if value.tzinfo is None and reference.tzinfo is not None:
            reference = reference.replace(tzinfo=None)
        elif value.tzinfo is not None and reference.tzinfo is None:
            reference = reference.replace(tzinfo=value.tzinfo)
        return value > reference

    @staticmethod
    def _channel_supports_item(channel: Channel, item: GenerationItem) -> bool:
        if item.job.mode not in channel.capabilities.modes:
            return False
        if item.job.output_format not in channel.capabilities.formats:
            return False
        try:
            channel.get_model(item.job.model)
        except ValueError:
            return False
        references = getattr(item.job, "references", ())
        if len(references) > channel.capabilities.max_reference_images:
            return False
        reference_bytes = []
        for reference in references:
            asset = getattr(reference, "asset", None)
            byte_count = int(getattr(asset, "byte_count", 0) or 0)
            reference_bytes.append(byte_count)
            if byte_count > channel.capabilities.max_reference_image_mb * 1024 * 1024:
                return False
        if sum(reference_bytes) > channel.capabilities.max_reference_total_mb * 1024 * 1024:
            return False
        return True

    @staticmethod
    def _provider_fields(item: GenerationItem, job: GenerationJob) -> tuple[str, str]:
        identifier = str(item.channel_id or "").strip()
        if not identifier or identifier == AUTO_CHANNEL_ID:
            return AUTO_CHANNEL_ID, AUTO_CHANNEL_LABEL
        return identifier, str(item.channel_label or job.channel_label or identifier)

    def _process_item(self, item_id: str, attempt_id: str | None = None) -> None:
        started = time.monotonic()
        attempt_status = "unknown"
        attempt_error: dict[str, object] = {}
        with self.app.app_context():
            attempt_id = attempt_id or self._future_attempt_ids.get(item_id)
            attempt = (
                self._attempt_for_item(attempt_id, item_id)
                if attempt_id is not None
                else self._running_attempt(item_id)
            )
            if attempt is None or attempt.claimed_by != self.worker_id:
                return
            attempt_id = attempt.id
            item = db.session.scalar(
                select(GenerationItem)
                .options(
                    selectinload(GenerationItem.job).selectinload(GenerationJob.references),
                )
                .where(GenerationItem.id == item_id)
            )
            if item is None:
                self._finalize_attempt(
                    item_id, attempt_id=attempt_id, status="unknown", started=started
                )
                db.session.remove()
                return
            if item.cancel_requested_at or item.job.cancel_requested_at:
                if self._owns_claim(item, attempt_id):
                    with self._settlement_lock:
                        self._settle_canceled(item_id, started, attempt_id)
                self._finalize_attempt(
                    item_id, attempt_id=attempt_id, status="canceled", started=started
                )
                db.session.remove()
                return
            try:
                channel = self.channels.get(item.channel_id)
                references = self._request_references(item)
                db.session.expire_all()
                latest_item = db.session.scalar(
                    select(GenerationItem)
                    .options(selectinload(GenerationItem.job))
                    .where(GenerationItem.id == item_id)
                )
                if (
                    latest_item is None
                    or not self._owns_claim(latest_item, attempt_id)
                    or latest_item.cancel_requested_at
                    or latest_item.job.cancel_requested_at
                ):
                    with self._settlement_lock:
                        self._settle_canceled(item_id, started, attempt_id)
                    attempt_status = "canceled"
                    return
                item = latest_item
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
                        transparent_background=False,
                        references=references,
                        idempotency_key=attempt.idempotency_key,
                    ),
                )
                content = result.content
                try:
                    self.storage.inspect(content)
                except InvalidImageError as exc:
                    raise ProviderError(
                        str(exc),
                        code="invalid_response",
                        request_id=result.request_id,
                        provider_completed=True,
                    ) from exc
                self._record_channel_success(channel, item_id, result.request_id, attempt_id)
                with self._settlement_lock:
                    self._settle_success(item_id, content, result.request_id, started, attempt_id)
                attempt_status = "succeeded"
            except ProviderError as exc:
                attempt_status = (
                    "downstream_failed"
                    if exc.provider_completed
                    else "unknown"
                    if exc.code in {"timeout", "connection_error"}
                    else "failed"
                )
                attempt_error = {
                    "code": exc.code,
                    "message": str(exc),
                    "upstream_status": exc.status_code,
                    "upstream_request_id": exc.request_id,
                }
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code=exc.code,
                        message=str(exc),
                        upstream_status=exc.status_code,
                        upstream_request_id=exc.request_id,
                        started=started,
                        details=exc.details,
                        retryable=(
                            not exc.provider_completed and self._provider_error_is_retryable(exc)
                        ),
                        result_unknown=(
                            not exc.provider_completed
                            and exc.code in {"timeout", "connection_error"}
                        ),
                        record_channel_failure=(
                            not exc.provider_completed
                            and exc.code in {"timeout", "connection_error"}
                        ),
                        attempt_id=attempt_id,
                    )
            except (StorageError, OSError) as exc:
                self._pause_dependency("storage", exc)
                attempt_status = "failed"
                attempt_error = {
                    "code": "storage_error",
                    "message": str(exc),
                }
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code="storage_error",
                        message=str(exc),
                        upstream_status=None,
                        upstream_request_id="",
                        started=started,
                        details={"exception_type": exc.__class__.__name__},
                        attempt_id=attempt_id,
                    )
            except Exception as exc:
                LOGGER.exception("生成任务发生未预期异常：%s", item_id)
                attempt_status = "unknown"
                attempt_error = {
                    "code": "internal_error",
                    "message": f"内部错误：{exc.__class__.__name__}",
                }
                with self._settlement_lock:
                    self._settle_failure(
                        item_id,
                        code="internal_error",
                        message=f"内部错误：{exc.__class__.__name__}",
                        upstream_status=None,
                        upstream_request_id="",
                        started=started,
                        details={"exception_type": exc.__class__.__name__},
                        attempt_id=attempt_id,
                    )
            finally:
                self._finalize_attempt(
                    item_id,
                    attempt_id=attempt_id,
                    status=attempt_status,
                    started=started,
                    **attempt_error,
                )
                db.session.remove()

    def _request_references(self, item: GenerationItem) -> tuple[ReferencePayload, ...]:
        return tuple(
            ReferencePayload(
                filename=reference.asset.original_name,
                content=self.storage.read_bytes(reference.asset.storage_path),
                mime_type=reference.asset.mime_type,
            )
            for reference in item.job.references
        )

    def _record_channel_success(
        self,
        channel: Channel,
        item_id: str,
        request_id: str,
        attempt_id: str | None = None,
    ) -> None:
        item = db.session.get(GenerationItem, item_id, populate_existing=True)
        attempt = (
            self._attempt_for_item(attempt_id, item_id, lock=True)
            if attempt_id is not None
            else self._running_attempt(item_id, lock=True)
        )
        if attempt is None or attempt.claimed_by != self.worker_id:
            db.session.rollback()
            return
        attempt.provider_completed = True
        attempt.upstream_request_id = request_id[:255]
        state = db.session.scalar(
            select(ChannelCircuitState)
            .where(ChannelCircuitState.channel_id == channel.identifier)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if state is None and not (item and item.circuit_probe):
            return
        if state is not None and state.open_until is not None and not (item and item.circuit_probe):
            return
        was_open = bool(state and state.open_until)
        if state is not None:
            state.failure_count = 0
            state.failure_window_started_at = None
            state.open_until = None
        if item is not None:
            item.circuit_probe = False
        if was_open:
            self.runtime_logs.record(
                category="worker",
                event="worker.channel_circuit_closed",
                status="success",
                message="生图渠道探测成功，已恢复调度",
                source="worker",
                item_id=item_id,
                provider_id=channel.identifier,
                provider_label=channel.label,
            )
        db.session.commit()

    def _running_attempt(
        self,
        item_id: str,
        *,
        lock: bool = False,
    ) -> GenerationAttempt | None:
        query = (
            select(GenerationAttempt)
            .where(
                GenerationAttempt.item_id == item_id,
                GenerationAttempt.status == "running",
            )
            .order_by(GenerationAttempt.attempt_number.desc())
            .limit(1)
        )
        if lock:
            query = query.with_for_update()
        return db.session.scalar(query)

    def _attempt_for_item(
        self,
        attempt_id: str,
        item_id: str,
        *,
        lock: bool = False,
    ) -> GenerationAttempt | None:
        query = select(GenerationAttempt).where(
            GenerationAttempt.id == attempt_id,
            GenerationAttempt.item_id == item_id,
            GenerationAttempt.status == "running",
        )
        if lock:
            query = query.with_for_update()
        return db.session.scalar(query)

    def _finalize_attempt(
        self,
        item_id: str,
        *,
        status: str,
        started: float,
        code: object = "",
        message: object = "",
        upstream_status: object = None,
        upstream_request_id: object = "",
        attempt_id: str | None = None,
    ) -> None:
        db.session.expire_all()
        attempt = (
            self._attempt_for_item(attempt_id, item_id, lock=True)
            if attempt_id is not None
            else self._running_attempt(item_id, lock=True)
        )
        if attempt is None or attempt.claimed_by != self.worker_id:
            db.session.rollback()
            return
        item = db.session.get(GenerationItem, item_id, populate_existing=True)
        if status == "succeeded":
            if item is not None and item.status == "succeeded":
                final_status = "succeeded"
            elif item is not None and item.status == "canceled":
                final_status = "discarded" if attempt.provider_completed else "canceled"
            else:
                final_status = "downstream_failed" if attempt.provider_completed else "unknown"
        elif status == "canceled":
            final_status = "discarded" if attempt.provider_completed else "canceled"
        elif status == "failed" and attempt.provider_completed:
            final_status = "downstream_failed"
        else:
            final_status = status
        attempt.status = final_status
        attempt.claimed_by = None
        attempt.heartbeat_at = None
        attempt.completed_at = utcnow()
        attempt.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
        if code:
            attempt.error_code = str(code)[:80]
        if message:
            attempt.error_message = str(message)[:1000]
        if upstream_status is not None:
            attempt.upstream_status = int(upstream_status)
        if upstream_request_id:
            attempt.upstream_request_id = str(upstream_request_id)[:255]
        db.session.commit()

    @staticmethod
    def _provider_error_is_retryable(error: ProviderError) -> bool:
        if error.code in {"timeout", "connection_error"}:
            return False
        if error.code in {
            "adapter_error",
            "invalid_response",
        }:
            return True
        status = error.status_code
        return bool(
            status is not None and (status >= 500 or status in {401, 403, 404, 408, 409, 425, 429})
        )

    def _settle_success(
        self,
        item_id: str,
        content: bytes,
        request_id: str,
        started: float,
        attempt_id: str | None = None,
    ) -> None:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None or not self._owns_claim(preview, attempt_id):
            return
        if preview.cancel_requested_at or preview.status == "canceling":
            self._settle_canceled(item_id, started, attempt_id)
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
            if item is None or not self._owns_claim(item, attempt_id):
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
            provider_id, provider_label = self._provider_fields(item, job)
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
                provider_id=provider_id,
                provider_label=provider_label,
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
        retryable: bool = False,
        record_channel_failure: bool = False,
        result_unknown: bool = False,
        attempt_id: str | None = None,
    ) -> None:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None or not self._owns_claim(preview, attempt_id):
            return
        attempt = (
            self._attempt_for_item(attempt_id, item_id, lock=True)
            if attempt_id is not None
            else self._running_attempt(item_id, lock=True)
        )
        if attempt is None or attempt.claimed_by != self.worker_id:
            db.session.rollback()
            return
        user = self.billing.lock_user(preview.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None or not self._owns_claim(item, attempt_id):
            db.session.rollback()
            return
        job = db.session.scalar(
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == item.job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        provider_id, provider_label = self._provider_fields(item, job)
        will_retry = False
        circuit_opened = False
        attempt_number = len(item.attempted_channel_ids or [])
        if item.cancel_requested_at or item.status == "canceling" or job.cancel_requested_at:
            self._mark_canceled(user, job, item, started)
        else:
            attempted = list(item.attempted_channel_ids or [])
            if provider_id not in {"", AUTO_CHANNEL_ID} and provider_id not in attempted:
                attempted.append(provider_id)
            item.attempted_channel_ids = attempted
            item.circuit_probe = False
            attempt_number = len(attempted)
            if retryable or record_channel_failure:
                circuit_opened = self._record_channel_failure(item, provider_id, provider_label)
            if retryable:
                will_retry = self._can_retry_item(item, attempted)
            if will_retry:
                self._reset_item_for_retry(item)
                job.completed_at = None
                attempt.status = "failed"
                attempt.claimed_by = None
                attempt.heartbeat_at = None
                attempt.completed_at = utcnow()
                attempt.elapsed_seconds = Decimal(str(round(time.monotonic() - started, 3)))
                attempt.error_code = code[:80]
                attempt.error_message = message[:1000]
                if upstream_status is not None:
                    attempt.upstream_status = upstream_status
                if upstream_request_id:
                    attempt.upstream_request_id = upstream_request_id[:255]
            else:
                item.status = "interrupted" if result_unknown else "failed"
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
            provider_id=provider_id,
            provider_label=provider_label,
            model=job.model,
            error_code=code,
            http_status=upstream_status,
            upstream_request_id=upstream_request_id,
            elapsed_seconds=elapsed_seconds,
            details={
                "diagnostics": details or {},
                "attempt": attempt_number,
                "max_attempts": self.channels.queue.max_channel_attempts,
                "will_retry": will_retry,
                "circuit_opened": circuit_opened,
                "result_unknown": result_unknown,
                "attempt_status": "unknown"
                if record_channel_failure and not retryable
                else "failed",
            },
        )
        db.session.commit()

    def _record_channel_failure(
        self,
        item: GenerationItem,
        provider_id: str,
        provider_label: str,
    ) -> bool:
        try:
            channel = self.channels.get(provider_id, require_available=False)
        except ValueError:
            return False
        state = db.session.scalar(
            select(ChannelCircuitState)
            .where(ChannelCircuitState.channel_id == provider_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if state is None:
            try:
                with db.session.begin_nested():
                    state = ChannelCircuitState(channel_id=provider_id)
                    db.session.add(state)
                    db.session.flush()
            except IntegrityError:
                state = db.session.scalar(
                    select(ChannelCircuitState)
                    .where(ChannelCircuitState.channel_id == provider_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if state is None:
                    raise
        now = utcnow()
        was_half_open = state.open_until is not None
        window_expired = bool(
            state.failure_window_started_at
            and self._time_is_after(
                now,
                state.failure_window_started_at
                + timedelta(seconds=channel.limits.failure_window_seconds),
            )
        )
        if state.failure_window_started_at is None or window_expired:
            state.failure_window_started_at = now
            state.failure_count = 1
        else:
            state.failure_count += 1
        should_open = was_half_open or state.failure_count >= channel.limits.failure_threshold
        if not should_open:
            return False
        state.open_until = now + timedelta(seconds=channel.limits.circuit_breaker_seconds)
        self.runtime_logs.record(
            category="worker",
            event="worker.channel_circuit_opened",
            status="error",
            level="warning",
            message="生图渠道连续失败，已暂停自动调度",
            source="worker",
            item_id=item.id,
            provider_id=provider_id,
            provider_label=provider_label,
            details={
                "failure_count": state.failure_count,
                "failure_threshold": channel.limits.failure_threshold,
                "open_until": state.open_until.isoformat(),
            },
        )
        return True

    def _can_retry_item(self, item: GenerationItem, attempted: list[str]) -> bool:
        if len(attempted) >= self.channels.queue.max_channel_attempts:
            return False
        routing = item.job.workflow.get("channel_routing") if item.job.workflow else None
        if isinstance(routing, dict) and routing.get("mode") == "selected":
            return False
        attempted_ids = set(attempted)
        return any(
            channel.identifier not in attempted_ids
            and channel.configured
            and channel.price_rmb <= item.job.price_per_image_rmb
            and self._channel_supports_item(channel, item)
            for channel in self.channels.list(include_disabled=False)
        )

    @staticmethod
    def _reset_item_for_retry(item: GenerationItem) -> None:
        item.status = "queued"
        item.channel_id = AUTO_CHANNEL_ID
        item.channel_label = AUTO_CHANNEL_LABEL
        item.provider_price_rmb = money(0)
        item.claimed_by = None
        item.heartbeat_at = None
        item.started_at = None
        item.completed_at = None
        item.estimated_seconds = None
        item.error_code = None
        item.error_message = None
        item.upstream_status = None
        item.upstream_request_id = None
        item.elapsed_seconds = None

    def _settle_canceled(self, item_id: str, started: float, attempt_id: str | None = None) -> None:
        db.session.expire_all()
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None or not self._owns_claim(preview, attempt_id):
            return
        user = self.billing.lock_user(preview.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None or not self._owns_claim(item, attempt_id):
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
        preview = db.session.get(GenerationItem, item_id, populate_existing=True)
        if preview is None:
            db.session.rollback()
            return
        user = self.billing.lock_user(preview.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if item is None:
            db.session.rollback()
            return
        job = db.session.scalar(
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == item.job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            job is None
            or item.status != "queued"
            or item.cancel_requested_at is not None
            or job.cancel_requested_at is not None
        ):
            db.session.rollback()
            return
        item.status = "failed"
        item.error_code = "channel_unavailable"
        item.error_message = "渠道已禁用或 API Key 未配置"
        item.completed_at = utcnow()
        self.billing.release(user, job, money(job.price_per_image_rmb))
        self.generations.refresh_job_status(job)
        provider_id, provider_label = self._provider_fields(item, job)
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
            provider_id=provider_id,
            provider_label=provider_label,
            model=job.model,
            error_code=item.error_code,
        )
        db.session.commit()

    @staticmethod
    def _active_status(item: GenerationItem) -> bool:
        return item.status in {"running", "canceling"}

    def _owns_claim(self, item: GenerationItem, attempt_id: str | None = None) -> bool:
        if not self._active_status(item) or item.claimed_by != self.worker_id:
            return False
        if attempt_id is None:
            return True
        return (
            db.session.scalar(
                select(GenerationAttempt.id).where(
                    GenerationAttempt.id == attempt_id,
                    GenerationAttempt.item_id == item.id,
                    GenerationAttempt.status == "running",
                    GenerationAttempt.claimed_by == self.worker_id,
                )
            )
            is not None
        )

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
            attempt_ids = tuple(
                attempt_id
                for item_id in item_ids
                if (attempt_id := self._future_attempt_ids.get(item_id)) is not None
            )
            if attempt_ids:
                db.session.execute(
                    update(GenerationAttempt)
                    .where(
                        GenerationAttempt.id.in_(attempt_ids),
                        GenerationAttempt.claimed_by == self.worker_id,
                        GenerationAttempt.status == "running",
                    )
                    .values(heartbeat_at=now)
                )
            db.session.execute(
                update(GenerationItem)
                .where(
                    GenerationItem.id.in_(item_ids),
                    GenerationItem.claimed_by == self.worker_id,
                    GenerationItem.status.in_(["running", "canceling"]),
                )
                .values(heartbeat_at=now)
            )
        background_result_ids = tuple(self._background_removal_futures)
        if background_result_ids:
            db.session.execute(
                update(BackgroundRemovalResult)
                .where(
                    BackgroundRemovalResult.id.in_(background_result_ids),
                    BackgroundRemovalResult.claimed_by == self.worker_id,
                    BackgroundRemovalResult.status == "running",
                )
                .values(heartbeat_at=now)
            )
        db.session.commit()

    def _recover_orphaned_items(self, *, immediate: bool) -> None:
        cutoff = utcnow() - timedelta(minutes=self.channels.queue.stale_running_minutes)
        attempt_conditions = [GenerationAttempt.status == "running"]
        if immediate:
            attempt_conditions.append(
                or_(
                    GenerationAttempt.claimed_by.is_(None),
                    GenerationAttempt.claimed_by != self.worker_id,
                )
            )
        else:
            attempt_conditions.append(
                or_(
                    GenerationAttempt.heartbeat_at.is_(None),
                    GenerationAttempt.heartbeat_at < cutoff,
                )
            )
        attempt_ids = list(
            db.session.scalars(select(GenerationAttempt.id).where(*attempt_conditions))
        )
        recovered = 0
        for attempt_id in attempt_ids:
            if self._recover_orphaned_attempt(attempt_id, cutoff=cutoff, immediate=immediate):
                recovered += 1

        conditions = [
            GenerationItem.status.in_(["running", "canceling"]),
            ~select(GenerationAttempt.id)
            .where(
                GenerationAttempt.item_id == GenerationItem.id,
                GenerationAttempt.status == "running",
            )
            .exists(),
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
        for item_id in item_ids:
            if self._recover_orphaned_item(item_id, cutoff=cutoff, immediate=immediate):
                recovered += 1
        if recovered:
            LOGGER.warning("已恢复 %d 个孤立的生成任务", recovered)
        self._recover_orphaned_background_removals(cutoff=cutoff, immediate=immediate)

    def _recover_crashed_background_removal(self, result_id: str) -> None:
        self._recover_background_removal(
            result_id,
            cutoff=utcnow(),
            immediate=True,
        )

    def _recover_orphaned_background_removals(self, *, cutoff, immediate: bool) -> None:
        conditions = [BackgroundRemovalResult.status == "running"]
        if immediate:
            conditions.append(
                or_(
                    BackgroundRemovalResult.claimed_by.is_(None),
                    BackgroundRemovalResult.claimed_by != self.worker_id,
                )
            )
        else:
            conditions.append(
                or_(
                    BackgroundRemovalResult.heartbeat_at.is_(None),
                    BackgroundRemovalResult.heartbeat_at < cutoff,
                )
            )
        result_ids = list(db.session.scalars(select(BackgroundRemovalResult.id).where(*conditions)))
        recovered = sum(
            self._recover_background_removal(
                result_id,
                cutoff=cutoff,
                immediate=immediate,
            )
            for result_id in result_ids
        )
        if recovered:
            LOGGER.warning("已恢复 %d 个孤立的透明化任务", recovered)

    def _recover_background_removal(self, result_id: str, *, cutoff, immediate: bool) -> bool:
        db.session.expire_all()
        result = db.session.scalar(
            select(BackgroundRemovalResult)
            .options(
                selectinload(BackgroundRemovalResult.run).selectinload(BackgroundRemovalRun.results)
            )
            .where(BackgroundRemovalResult.id == result_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if result is None or result.status != "running":
            db.session.rollback()
            return False
        live_future = result.id in self._background_removal_futures
        recoverable_claim = result.claimed_by != self.worker_id or not live_future
        comparison_cutoff = cutoff
        if result.heartbeat_at is not None and result.heartbeat_at.tzinfo is None:
            comparison_cutoff = cutoff.replace(tzinfo=None)
        stale_claim = result.heartbeat_at is None or result.heartbeat_at < comparison_cutoff
        if not recoverable_claim or (not immediate and not stale_claim):
            db.session.rollback()
            return False

        result.status = "queued"
        result.claimed_by = None
        result.heartbeat_at = None
        result.started_at = None
        result.completed_at = None
        result.elapsed_seconds = None
        result.error_code = None
        result.error_message = None
        self.background_removal.refresh_run_status(result.run)
        db.session.commit()
        return True

    def _recover_crashed_attempt(self, item_id: str, *, attempt_id: str | None = None) -> None:
        if attempt_id is None:
            attempt_id = db.session.scalar(
                select(GenerationAttempt.id)
                .where(
                    GenerationAttempt.item_id == item_id,
                    GenerationAttempt.status == "running",
                )
                .order_by(GenerationAttempt.attempt_number.desc())
                .limit(1)
            )
        if attempt_id is not None:
            self._recover_orphaned_attempt(
                attempt_id,
                cutoff=utcnow(),
                immediate=True,
            )

    def _recover_orphaned_attempt(self, attempt_id: str, *, cutoff, immediate: bool) -> bool:
        db.session.expire_all()
        attempt = db.session.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.id == attempt_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if attempt is None or attempt.status != "running":
            db.session.rollback()
            return False
        mapped_attempt_id = self._future_attempt_ids.get(attempt.item_id)
        live_future = attempt.item_id in self._futures and (
            mapped_attempt_id is None or mapped_attempt_id == attempt_id
        )
        recoverable_claim = attempt.claimed_by != self.worker_id or not live_future
        comparison_cutoff = cutoff
        if attempt.heartbeat_at is not None and attempt.heartbeat_at.tzinfo is None:
            comparison_cutoff = cutoff.replace(tzinfo=None)
        stale_claim = attempt.heartbeat_at is None or attempt.heartbeat_at < comparison_cutoff
        if not recoverable_claim or (not immediate and not stale_claim):
            db.session.rollback()
            return False

        user = self.billing.lock_user(attempt.user_id)
        item = db.session.scalar(
            select(GenerationItem)
            .where(GenerationItem.id == attempt.item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        replacement_attempt_id = db.session.scalar(
            select(GenerationAttempt.id)
            .where(
                GenerationAttempt.item_id == attempt.item_id,
                GenerationAttempt.id != attempt.id,
                GenerationAttempt.status == "running",
            )
            .limit(1)
        )
        job = None
        if item is not None and self._active_status(item) and replacement_attempt_id is None:
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
            item.claimed_by = None
            item.heartbeat_at = None
            self.billing.release(user, job, money(job.price_per_image_rmb))
            self.generations.refresh_job_status(job)

        attempt.status = "downstream_failed" if attempt.provider_completed else "unknown"
        attempt.error_code = "worker_interrupted"
        attempt.error_message = "Worker 中断，渠道调用结果未知"
        attempt.claimed_by = None
        attempt.heartbeat_at = None
        attempt.completed_at = utcnow()
        provider_id = attempt.channel_id
        provider_label = attempt.channel_label or provider_id
        self.runtime_logs.record(
            category="worker",
            event="worker.recovered_attempt",
            status="error",
            level="warning",
            message=attempt.error_message,
            source="worker",
            user_id=attempt.user_id,
            user_label=user.display_name or user.username,
            workspace_id=job.workspace_id if job is not None else "",
            workspace_label=job.workspace.name if job is not None else "",
            job_id=job.id if job is not None else "",
            item_id=attempt.item_id,
            provider_id=provider_id,
            provider_label=provider_label,
            model=job.model if job is not None else "",
            error_code=attempt.error_code,
            details={
                "worker_id": self.worker_id,
                "immediate": immediate,
                "provider_completed": attempt.provider_completed,
                "capacity_expires_at": attempt.capacity_expires_at.isoformat(),
            },
        )
        db.session.commit()
        return True

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
        provider_id, provider_label = self._provider_fields(item, job)
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
            provider_id=provider_id,
            provider_label=provider_label,
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
