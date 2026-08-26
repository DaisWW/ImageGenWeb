from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ...config.channels import (
    AUTO_CHANNEL_ID,
    AUTO_CHANNEL_LABEL,
    MIXED_CHANNEL_ID,
    MIXED_CHANNEL_LABEL,
    Channel,
    ChannelRegistry,
)
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
        references = self.validator.load_references(workspace, request.reference_ids)
        routing_channels, selected_model, normalized_size = self._resolve_routing(
            request,
            workspace.kind,
            references,
        )
        requested_count = request.batch_count
        item_prompts = tuple(
            str(item).strip()
            for item in (
                request.item_prompts or tuple(request.prompt for _ in range(requested_count))
            )
        )

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
        self._ensure_workspace_generation_idle(workspace.id)
        self._ensure_queue_capacity(user_id, requested_count)

        # Reserve the most expensive eligible provider up front.  A routed
        # item later records its concrete provider price and settlement charges
        # that amount while releasing the full per-item reservation.
        reservation_unit_price = max(channel.price_rmb for channel in routing_channels)
        primary_channel = routing_channels[0]
        reserved = money(reservation_unit_price * requested_count)
        self.billing.reserve(user, reserved)
        workflow = sanitize_workflow(request.workflow)
        workflow["channel_routing"] = {
            "mode": "priority",
            "candidate_ids": [channel.identifier for channel in routing_channels],
            "candidate_labels": [channel.label for channel in routing_channels],
        }
        job = GenerationJob(
            user_id=user.id,
            workspace_id=workspace.id,
            # Keep a useful summary for legacy consumers.  The item-level
            # channel is authoritative once the Worker has routed it.
            channel_id=primary_channel.identifier,
            channel_label=primary_channel.label,
            channel_config_version=self.channels.version,
            kind=workspace.kind,
            mode=request.mode,
            prompt=request.prompt.strip(),
            model=selected_model.identifier,
            size=normalized_size,
            quality=request.quality,
            workflow=workflow,
            output_format=request.output_format,
            compression=request.compression,
            transparent_background=request.transparent_background,
            requested_count=requested_count,
            price_per_image_rmb=money(reservation_unit_price),
            reserved_rmb=reserved,
            charged_rmb=money(0),
            status="queued",
        )
        db.session.add(job)
        try:
            db.session.flush()
        except IntegrityError as exc:
            db.session.rollback()
            raise self._workspace_active_error() from exc
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
                    channel_id=AUTO_CHANNEL_ID,
                    channel_label=AUTO_CHANNEL_LABEL,
                    position=position,
                    prompt=item_prompts[position],
                    status="queued",
                    charged_rmb=money(0),
                    provider_price_rmb=money(0),
                )
            )
        workspace.settings = sanitize_workspace_settings(
            {
                **(workspace.settings or {}),
                "mode": request.mode,
                "prompt": request.prompt,
                "channel_id": AUTO_CHANNEL_ID,
                "model": selected_model.identifier,
                "size": normalized_size,
                "output_format": request.output_format,
                "compression": request.compression,
                "transparent_background": request.transparent_background,
                "batch_count": request.batch_count,
                "generation_stage": workflow["generation_stage"],
                "prompt_draft_id": workflow["prompt_draft_id"],
                "creative_direction_id": workflow["creative_direction_id"],
                "generation_strategy": workflow.get("generation_strategy", "sample"),
            },
            self.settings.runtime(),
        )
        db.session.commit()
        return self.get_job(job.id, user_id=user.id)

    def _resolve_routing(
        self,
        request: SubmitGeneration,
        workspace_kind: str,
        references: list,
    ) -> tuple[list[Channel], object, str]:
        """Return the configured channels that can execute this request.

        Channel selection is deliberately deferred to the Worker so queued
        items can use a lower-priority provider when the preferred provider is
        full.  Submission still validates every eligible provider and chooses
        one model shared by the largest number of providers.
        """

        configured = [
            channel for channel in self.channels.list(include_disabled=False) if channel.configured
        ]
        if not configured:
            raise ServiceError(
                "暂无可用生图渠道",
                code="channel_unavailable",
                status_code=503,
            )

        requested_model = str(request.model or "").strip()
        model_ids: list[str] = []
        if requested_model:
            model_ids = [requested_model]
        else:
            # Prefer a model shared by the largest number of channels.  Ties
            # retain the configured priority order so the first provider stays
            # deterministic.
            for channel in configured:
                for model in channel.models:
                    if model.enabled and model.identifier not in model_ids:
                        model_ids.append(model.identifier)

        first_error: ServiceError | None = None
        best: tuple[list[Channel], object, str] | None = None
        for model_id in model_ids:
            candidates: list[Channel] = []
            normalized_size = ""
            model = None
            for channel in configured:
                try:
                    selected = channel.get_model(model_id)
                    normalized = self.validator.validate_request(
                        channel,
                        request,
                        workspace_kind,
                    )
                    self.validator.validate_references(channel, request.mode, references)
                except ValueError as exc:
                    if first_error is None:
                        first_error = ServiceError(str(exc))
                    continue
                except ServiceError as exc:
                    if first_error is None:
                        first_error = exc
                    continue
                if model is None:
                    model = selected
                    normalized_size = normalized
                candidates.append(channel)
            if not candidates or model is None:
                continue
            trial = (candidates, model, normalized_size)
            if best is None or len(candidates) > len(best[0]):
                best = trial

        if best is not None:
            return best
        if first_error is not None:
            raise first_error
        raise ServiceError(
            "没有渠道支持当前生成请求",
            code="channel_unavailable",
            status_code=422,
        )

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

    def _ensure_workspace_generation_idle(self, workspace_id: str) -> None:
        active_job = db.session.scalar(
            select(GenerationJob.id)
            .where(
                GenerationJob.workspace_id == workspace_id,
                GenerationJob.status.in_(["queued", "running", "canceling"]),
            )
            .limit(1)
        )
        if active_job:
            raise self._workspace_active_error()

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
    def _workspace_active_error() -> ServiceError:
        return ServiceError(
            "当前工作站已有生成任务，请等待完成或先取消",
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
            job.status = "queued"
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
            key=lambda value: (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            ),
            default=utcnow(),
        )

    @staticmethod
    def refresh_job_channel_summary(job: GenerationJob) -> None:
        """Keep the legacy job-level channel fields useful for routed jobs."""

        assigned = []
        seen: set[str] = set()
        for item in job.items:
            identifier = str(item.channel_id or "").strip()
            if not identifier or identifier == AUTO_CHANNEL_ID or identifier in seen:
                continue
            seen.add(identifier)
            assigned.append((identifier, str(item.channel_label or "").strip()))
        if not assigned:
            return
        if len(assigned) == 1:
            job.channel_id, job.channel_label = assigned[0]
            return
        job.channel_id = MIXED_CHANNEL_ID
        job.channel_label = MIXED_CHANNEL_LABEL
