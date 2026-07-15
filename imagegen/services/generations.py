from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta, timezone
from decimal import Decimal
from statistics import fmean

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..config.channels import Channel, ChannelRegistry
from ..errors import ServiceError
from ..extensions import db
from ..models import (
    Asset,
    GenerationItem,
    GenerationJob,
    GenerationQueueState,
    GenerationReference,
    RuntimeLog,
    User,
    Workspace,
    utcnow,
)
from .billing import BillingService
from .common import money, normalize_image_size
from .settings import SystemSettingsService
from .workspace_settings import sanitize_workspace_settings

_DURATION_SAMPLE_LIMIT = 50
_DURATION_SAMPLE_TARGET = 8
_DURATION_TRIM_RATIO = 0.1


def _duration_values(values: Iterable[Decimal | float | int | None]) -> list[float]:
    durations = []
    for value in values:
        if value is None:
            continue
        duration = float(value)
        if math.isfinite(duration) and duration > 0:
            durations.append(duration)
    return durations


def _robust_duration_estimate(
    samples: Iterable[Decimal | float | int | None], baseline: float
) -> float:
    ordered = sorted(_duration_values(samples))
    if not ordered:
        return baseline

    sample_count = len(ordered)
    trim_count = int(sample_count * _DURATION_TRIM_RATIO)
    trimmed = ordered[trim_count:-trim_count] if trim_count else ordered
    observed = fmean(trimmed)
    confidence = min(1.0, sample_count / _DURATION_SAMPLE_TARGET)
    return baseline + (observed - baseline) * confidence


@dataclass(frozen=True)
class SubmitGeneration:
    channel_id: str
    model: str
    mode: str
    prompt: str
    size: str
    quality: str
    output_format: str
    compression: int
    batch_count: int
    reference_ids: tuple[str, ...]
    transparent_background: bool = False
    frame_count: int = 8
    animation_fps: int = 8
    animation_loop: bool = True
    animation_format: str = "webp"


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
        normalized_size = self._validate_request(channel, request, workspace.kind)
        references = self._load_references(workspace, request.reference_ids)
        if workspace.kind == "animation":
            if not references:
                raise ServiceError("请先上传或选择一张母图")
            if request.mode != "img2img":
                raise ServiceError("帧动画工作站只能使用指定母图生成帧动画")
            if len(references) != 1:
                raise ServiceError("帧动画任务必须且只能选择一张母图")
            job_kind = "animation"
            requested_count = request.frame_count
        else:
            job_kind = "image"
            requested_count = request.batch_count
        self._validate_references(channel, request.mode, references)

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
                "quality": request.quality,
                "output_format": request.output_format,
                "compression": request.compression,
                "transparent_background": request.transparent_background,
                "batch_count": request.batch_count,
                "animation_frame_count": request.frame_count,
                "animation_fps": request.animation_fps,
                "animation_loop": request.animation_loop,
                "animation_format": request.animation_format,
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
            if item.status == "queued":
                item.status = "canceled"
                item.cancel_requested_at = now
                item.completed_at = now
                releasable += money(job.price_per_image_rmb)
            elif item.status == "running":
                item.status = "canceling"
                item.cancel_requested_at = now
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
        samples = self._duration_samples(job, exact=True)
        if len(samples) < _DURATION_SAMPLE_TARGET:
            related = self._duration_samples(job, exact=False)
            samples = (
                related
                if len(related) >= _DURATION_SAMPLE_TARGET
                else max(related, self._runtime_duration_samples(job), key=len)
            )

        estimate = _robust_duration_estimate(
            samples,
            baseline=float(channel.limits.estimated_seconds),
        )
        estimate = min(max(estimate, 10.0), float(channel.limits.timeout_seconds))
        return Decimal(str(round(estimate, 3)))

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

    def _duration_samples(self, job: GenerationJob, *, exact: bool) -> list[float]:
        kinds = (
            ("image", "animation_master")
            if job.kind in {"image", "animation_master"}
            else (job.kind,)
        )
        query = (
            select(GenerationItem.elapsed_seconds)
            .join(GenerationJob)
            .where(
                GenerationItem.status == "succeeded",
                GenerationItem.elapsed_seconds.is_not(None),
                GenerationJob.channel_id == job.channel_id,
                GenerationJob.model == job.model,
                GenerationJob.kind.in_(kinds),
                GenerationJob.mode == job.mode,
            )
            .order_by(GenerationItem.completed_at.desc())
            .limit(_DURATION_SAMPLE_LIMIT)
        )
        if exact:
            query = query.where(
                GenerationJob.size == job.size,
                GenerationJob.quality == job.quality,
            )
        return _duration_values(db.session.scalars(query))

    @staticmethod
    def _runtime_duration_samples(job: GenerationJob) -> list[float]:
        query = (
            select(RuntimeLog.elapsed_seconds)
            .where(
                RuntimeLog.category == "generation",
                RuntimeLog.event == "generation.provider",
                RuntimeLog.status == "success",
                RuntimeLog.elapsed_seconds.is_not(None),
                RuntimeLog.provider_id == job.channel_id,
                RuntimeLog.model == job.model,
            )
            .order_by(RuntimeLog.created_at.desc())
            .limit(_DURATION_SAMPLE_LIMIT)
        )
        return _duration_values(db.session.scalars(query))

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

    @staticmethod
    def _load_references(workspace: Workspace, reference_ids: tuple[str, ...]) -> list[Asset]:
        if len(reference_ids) != len(set(reference_ids)):
            raise ServiceError("垫图不能重复")
        if not reference_ids:
            return []
        assets = list(
            db.session.scalars(
                select(Asset).where(
                    Asset.workspace_id == workspace.id,
                    Asset.id.in_(reference_ids),
                    Asset.deleted_at.is_(None),
                )
            )
        )
        by_id = {asset.id: asset for asset in assets}
        if any(asset_id not in by_id for asset_id in reference_ids):
            raise ServiceError("选择的垫图不存在")
        return [by_id[asset_id] for asset_id in reference_ids]

    def _validate_request(
        self, channel: Channel, request: SubmitGeneration, workspace_kind: str
    ) -> str:
        runtime = self.settings.runtime()
        if workspace_kind not in {"image", "animation"}:
            raise ServiceError("工作站类型无效")
        if request.mode not in channel.capabilities.modes:
            raise ServiceError(f"{channel.label} 不支持当前生成模式")
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > runtime.max_prompt_characters:
            raise ServiceError(f"提示词长度必须在 1 到 {runtime.max_prompt_characters} 个字符之间")
        normalized_size = normalize_image_size(request.size)
        if request.quality not in channel.capabilities.qualities:
            raise ServiceError(f"{channel.label} 不支持质量 {request.quality}")
        if request.output_format not in channel.capabilities.formats:
            raise ServiceError(f"{channel.label} 不支持格式 {request.output_format}")
        if request.transparent_background and request.output_format not in {"png", "webp"}:
            raise ServiceError("透明背景仅支持 PNG 或 WebP 格式")
        if not 0 <= request.compression <= 100:
            raise ServiceError("压缩质量必须在 0 到 100 之间")
        if not 1 <= request.batch_count <= runtime.max_batch_images:
            raise ServiceError(f"单批生成张数必须在 1 到 {runtime.max_batch_images} 之间")
        if not 2 <= request.frame_count <= runtime.max_animation_frames:
            raise ServiceError(f"动画帧数必须在 2 到 {runtime.max_animation_frames} 之间")
        if not 1 <= request.animation_fps <= runtime.max_animation_fps:
            raise ServiceError(f"动画帧率必须在 1 到 {runtime.max_animation_fps} FPS 之间")
        if request.animation_format not in {"webp", "gif"}:
            raise ServiceError("动画导出格式仅支持 WebP 或 GIF")
        return normalized_size

    def _validate_references(self, channel: Channel, mode: str, references: list[Asset]) -> None:
        if mode == "img2img" and not references:
            raise ServiceError("垫图生图至少需要一张垫图")
        if mode == "text2img" and references:
            raise ServiceError("文生图任务不能携带垫图")
        runtime = self.settings.runtime()
        if any(asset.byte_count > runtime.max_attachment_bytes for asset in references):
            raise ServiceError(f"单张参考图不能超过 {runtime.max_attachment_mb} MiB")
        if sum(asset.byte_count for asset in references) > runtime.max_attachment_total_bytes:
            raise ServiceError(f"参考图合计不能超过 {runtime.max_attachment_total_mb} MiB")
        capabilities = channel.capabilities
        if len(references) > capabilities.max_reference_images:
            raise ServiceError(
                f"{channel.label} 最多支持 {capabilities.max_reference_images} 张垫图"
            )
        if any(
            asset.byte_count > capabilities.max_reference_image_mb * 1024 * 1024
            for asset in references
        ):
            raise ServiceError(
                f"{channel.label} 的单张垫图不能超过 {capabilities.max_reference_image_mb} MiB"
            )
        if (
            sum(asset.byte_count for asset in references)
            > capabilities.max_reference_total_mb * 1024 * 1024
        ):
            raise ServiceError(
                f"{channel.label} 的垫图合计不能超过 {capabilities.max_reference_total_mb} MiB"
            )
