from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ...config.channels import Channel, ChannelRegistry
from ...errors import ServiceError
from ...extensions import db
from ...models import (
    GenerationItem,
    GenerationJob,
    GenerationQueueState,
    GenerationReference,
    User,
    Workspace,
    utcnow,
)
from ..billing import BillingService
from ..common import money
from ..settings import SystemSettingsService
from ..workspace_settings import sanitize_workspace_settings
from .contracts import SubmitGeneration, sanitize_workflow
from .estimates import GenerationDurationEstimator
from .validation import GenerationRequestValidator


class GenerationService:
    def __init__(
        self,
        channels: ChannelRegistry,
        billing: BillingService,
        settings: SystemSettingsService,
    ):
        self.channels = channels
        self.billing = billing
        self.settings = settings
        self.validator = GenerationRequestValidator(settings)
        self.duration_estimator = GenerationDurationEstimator()

    def submit(
        self,
        user_id: int,
        workspace: Workspace,
        request: SubmitGeneration,
    ) -> GenerationJob:
        channel = self.channels.get(request.channel_id)
        try:
            selected_model = channel.get_model(request.model)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        normalized_size = self.validator.validate_request(channel, request, workspace.kind)
        references = self.validator.load_references(workspace, request.reference_ids)
        job_kind, requested_count = self.validator.job_shape(workspace.kind, request, references)
        self.validator.validate_references(channel, request.mode, references)

        user = self.billing.lock_user(user_id)
        if not user.is_active:
            raise ServiceError("账户已被禁用", status_code=403)
        locked_workspace_id = db.session.scalar(
            select(Workspace.id)
            .where(Workspace.id == workspace.id, Workspace.user_id == user_id)
            .with_for_update()
        )
        if locked_workspace_id is None:
            raise ServiceError("工作站不存在", status_code=404)
        self._ensure_workspace_generation_idle(workspace.id, workspace.kind)
        self._ensure_queue_capacity(user_id, requested_count)

        reserved = money(channel.price_rmb * requested_count)
        self.billing.reserve(user, reserved)
        workflow = sanitize_workflow(request.workflow)
        job = GenerationJob(
            user_id=user.id,
            workspace_id=workspace.id,
            channel_id=channel.identifier,
            channel_label=channel.label,
            channel_config_version=self.channels.version,
            kind=job_kind,
            mode=request.mode,
            prompt=request.prompt.strip(),
            model=selected_model.identifier,
            size=normalized_size,
            quality=request.quality,
            workflow=workflow,
            output_format=request.output_format,
            compression=request.compression,
            transparent_background=request.transparent_background,
            animation_fps=request.animation_fps,
            animation_loop=request.animation_loop,
            animation_format=request.animation_format,
            requested_count=requested_count,
            price_per_image_rmb=money(channel.price_rmb),
            reserved_rmb=reserved,
            charged_rmb=money(0),
            status="queued",
        )
        db.session.add(job)
        try:
            db.session.flush()
        except IntegrityError as exc:
            db.session.rollback()
            raise self._workspace_active_error(workspace.kind) from exc
        for position, asset in enumerate(references):
            db.session.add(
                GenerationReference(
                    job_id=job.id,
                    asset_id=asset.id,
                    position=position,
                )
            )
        for position in range(requested_count):
            db.session.add(
                GenerationItem(
                    job_id=job.id,
                    user_id=user.id,
                    channel_id=channel.identifier,
                    position=position,
                    status="queued",
                    charged_rmb=money(0),
                )
            )
        workspace.settings = sanitize_workspace_settings(
            {
                **(workspace.settings or {}),
                "mode": request.mode,
                "prompt": request.prompt,
                "channel_id": request.channel_id,
                "model": selected_model.identifier,
                "size": normalized_size,
                "output_format": request.output_format,
                "compression": request.compression,
                "transparent_background": request.transparent_background,
                "batch_count": request.batch_count,
                "animation_frame_count": request.frame_count,
                "animation_fps": request.animation_fps,
                "animation_loop": request.animation_loop,
                "animation_format": request.animation_format,
                "generation_stage": workflow["generation_stage"],
                "prompt_draft_id": workflow["prompt_draft_id"],
                "creative_direction_id": workflow["creative_direction_id"],
            },
            self.settings.runtime(),
        )
        db.session.commit()
        return self.get_job(job.id, user_id=user.id)

    def cancel(
        self,
        job_id: str,
        *,
        user_id: int | None = None,
        admin: bool = False,
    ) -> GenerationJob:
        user, job = self._lock_job_and_owner(job_id, user_id=user_id, admin=admin)
        if job.status in {"succeeded", "failed", "canceled", "partial"}:
            return job
        now = utcnow()
        job.cancel_requested_at = now
        releasable = Decimal("0")
        for item in job.items:
            if item.status not in {"queued", "running", "canceling"}:
                continue
            item.status = "canceled"
            item.cancel_requested_at = now
            item.completed_at = now
            item.claimed_by = None
            item.heartbeat_at = None
            releasable += money(job.price_per_image_rmb)
        self.billing.release(user, job, releasable)
        self.refresh_job_status(job)
        db.session.commit()
        return job

    def retry_animation(
        self,
        job_id: str,
        *,
        user_id: int,
    ) -> GenerationJob:
        user, job = self._lock_job_and_owner(job_id, user_id=user_id)
        if not job.is_animation_retryable:
            raise ServiceError(
                "当前帧动画任务不能继续生成",
                code="generation_not_retryable",
                status_code=409,
            )
        self._ensure_workspace_generation_idle(job.workspace_id, "animation")
        try:
            channel = self.channels.get(job.channel_id)
            channel.get_model(job.model)
        except ValueError as exc:
            raise ServiceError(str(exc), status_code=409) from exc

        retry_items = [item for item in job.items if item.status != "succeeded"]
        retry_count = len(retry_items)
        if not user.is_active:
            raise ServiceError("账户已被禁用", status_code=403)
        self._ensure_queue_capacity(user.id, retry_count)

        reserved = money(job.price_per_image_rmb * retry_count)
        self.billing.reserve(user, reserved)
        job.reserved_rmb = money(job.reserved_rmb + reserved)
        job.cancel_requested_at = None
        job.completed_at = None
        for item in retry_items:
            item.status = "queued"
            item.cancel_requested_at = None
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
        self.refresh_job_status(job)
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise self._workspace_active_error("animation") from exc
        return self.get_job(job.id, user_id=user.id)

    def get_job(
        self,
        job_id: str,
        *,
        user_id: int | None = None,
        admin: bool = False,
    ) -> GenerationJob:
        query = (
            select(GenerationJob)
            .options(
                selectinload(GenerationJob.items),
                selectinload(GenerationJob.references).selectinload(GenerationReference.asset),
                selectinload(GenerationJob.user),
            )
            .where(GenerationJob.id == job_id)
        )
        if not admin:
            query = query.where(GenerationJob.user_id == user_id)
        job = db.session.scalar(query)
        if job is None:
            raise ServiceError("生成任务不存在", status_code=404)
        return job

    def list_jobs(
        self,
        *,
        user_id: int | None = None,
        workspace_id: str | None = None,
        admin: bool = False,
        limit: int = 100,
    ) -> list[GenerationJob]:
        eager_options = [
            selectinload(GenerationJob.items),
            selectinload(GenerationJob.references).selectinload(GenerationReference.asset),
        ]
        if admin:
            eager_options.append(selectinload(GenerationJob.user))
        query = select(GenerationJob).options(*eager_options)
        if not admin or user_id is not None:
            query = query.where(GenerationJob.user_id == user_id)
        if workspace_id:
            query = query.where(GenerationJob.workspace_id == workspace_id)
        cutoff = utcnow() - timedelta(days=self.channels.queue.history_retention_days)
        query = query.where(
            (GenerationJob.completed_at.is_(None)) | (GenerationJob.completed_at >= cutoff)
        )
        query = query.order_by(GenerationJob.created_at.desc()).limit(min(max(limit, 1), 200))
        return list(db.session.scalars(query))

    def list_active_jobs(self, user_id: int) -> list[GenerationJob]:
        return list(
            db.session.scalars(
                select(GenerationJob)
                .options(selectinload(GenerationJob.items))
                .where(
                    GenerationJob.user_id == user_id,
                    GenerationJob.status.in_(("queued", "running", "canceling")),
                )
                .order_by(GenerationJob.created_at)
            )
        )

    def queue_item_counts(
        self,
        *,
        user_id: int | None = None,
        workspace_id: str | None = None,
    ) -> tuple[int, int]:
        query = (
            select(GenerationItem.status, func.count(GenerationItem.id))
            .join(GenerationJob)
            .where(GenerationItem.status.in_(("running", "canceling", "queued")))
        )
        if user_id is not None:
            query = query.where(GenerationJob.user_id == user_id)
        if workspace_id:
            query = query.where(GenerationJob.workspace_id == workspace_id)
        counts = dict(db.session.execute(query.group_by(GenerationItem.status)).all())
        return (
            int(counts.get("running", 0)) + int(counts.get("canceling", 0)),
            int(counts.get("queued", 0)),
        )

    def queue_positions(self) -> dict[str, int]:
        queued_ids = list(
            db.session.scalars(
                select(GenerationJob.id)
                .where(GenerationJob.status == "queued")
                .order_by(GenerationJob.created_at, GenerationJob.id)
            )
        )
        return {job_id: index + 1 for index, job_id in enumerate(queued_ids)}

    def estimate_seconds(self, job: GenerationJob, channel: Channel) -> Decimal:
        return self.duration_estimator.estimate_seconds(job, channel)

    def _ensure_workspace_generation_idle(self, workspace_id: str, workspace_kind: str) -> None:
        active_job = db.session.scalar(
            select(GenerationJob.id)
            .where(
                GenerationJob.workspace_id == workspace_id,
                GenerationJob.status.in_(["queued", "running", "canceling"]),
            )
            .limit(1)
        )
        if active_job:
            raise self._workspace_active_error(workspace_kind)

    def _ensure_queue_capacity(self, user_id: int, requested_count: int) -> None:
        lock_result = db.session.execute(
            update(GenerationQueueState)
            .where(GenerationQueueState.id == 1)
            .values(updated_at=utcnow())
        )
        if lock_result.rowcount != 1:
            raise RuntimeError("生成队列状态未初始化")
        user_queued = (
            db.session.scalar(
                select(func.count(GenerationItem.id)).where(
                    GenerationItem.user_id == user_id,
                    GenerationItem.status == "queued",
                )
            )
            or 0
        )
        global_queued = (
            db.session.scalar(
                select(func.count(GenerationItem.id)).where(GenerationItem.status == "queued")
            )
            or 0
        )
        queue = self.channels.queue
        if user_queued + requested_count > queue.max_queued_per_user:
            raise ServiceError("当前账户排队图片已达到上限", code="queue_full", status_code=429)
        if global_queued + requested_count > queue.max_queued_global:
            raise ServiceError("系统排队图片已达到上限", code="queue_full", status_code=429)

    def _lock_job_and_owner(
        self,
        job_id: str,
        *,
        user_id: int | None,
        admin: bool = False,
    ) -> tuple[User, GenerationJob]:
        owner_query = select(GenerationJob.user_id).where(GenerationJob.id == job_id)
        if not admin:
            owner_query = owner_query.where(GenerationJob.user_id == user_id)
        owner_id = db.session.scalar(owner_query)
        if owner_id is None:
            raise ServiceError("生成任务不存在", status_code=404)

        user = self.billing.lock_user(owner_id)
        job_query = (
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == job_id)
        )
        if not admin:
            job_query = job_query.where(GenerationJob.user_id == user_id)
        job = db.session.scalar(job_query.with_for_update())
        if job is None:
            raise ServiceError("生成任务不存在", status_code=404)
        return user, job

    @staticmethod
    def _workspace_active_error(workspace_kind: str) -> ServiceError:
        subject = "帧动画" if workspace_kind == "animation" else "图片"
        return ServiceError(
            f"当前工作站已有{subject}任务，请等待完成或先取消",
            code="workspace_generation_active",
            status_code=409,
        )

    @staticmethod
    def refresh_job_status(job: GenerationJob) -> None:
        statuses = [item.status for item in job.items]
        if any(status in {"running", "canceling"} for status in statuses):
            job.status = "canceling" if job.cancel_requested_at else "running"
            return
        if any(status == "queued" for status in statuses):
            job.status = (
                "running"
                if job.kind == "animation" and any(status == "succeeded" for status in statuses)
                else "queued"
            )
            return
        succeeded = statuses.count("succeeded")
        failed = statuses.count("failed") + statuses.count("interrupted")
        canceled = statuses.count("canceled")
        if succeeded == len(statuses):
            job.status = "succeeded"
        elif canceled == len(statuses):
            job.status = "canceled"
        elif succeeded:
            job.status = "partial"
        else:
            job.status = "failed" if failed else "canceled"
        completed_times = [item.completed_at for item in job.items if item.completed_at]
        job.completed_at = max(
            completed_times,
            key=lambda value: value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc),
            default=utcnow(),
        )
