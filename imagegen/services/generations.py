from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..config.channels import Channel, ChannelRegistry
from ..errors import ServiceError
from ..extensions import db
from ..models import (
    Asset,
    GenerationItem,
    GenerationJob,
    GenerationReference,
    Workspace,
    utcnow,
)
from .billing import BillingService
from .common import money, normalize_image_size
from .workspace_settings import sanitize_workspace_settings


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


class GenerationService:
    def __init__(self, channels: ChannelRegistry, billing: BillingService):
        self.channels = channels
        self.billing = billing

    def submit(
        self,
        user_id: int,
        workspace: Workspace,
        request: SubmitGeneration,
    ) -> GenerationJob:
        if db.session.scalar(
            select(GenerationJob.id)
            .where(
                GenerationJob.workspace_id == workspace.id,
                GenerationJob.status.in_(["queued", "running", "canceling"]),
            )
            .limit(1)
        ):
            raise ServiceError(
                "当前工作站已有图片任务，请等待完成或先取消",
                code="workspace_generation_active",
                status_code=409,
            )
        channel = self.channels.get(request.channel_id)
        try:
            selected_model = channel.get_model(request.model)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        normalized_size = self._validate_request(channel, request)
        references = self._load_references(workspace, request.reference_ids)
        self._validate_references(channel, request.mode, references)

        user = self.billing.lock_user(user_id)
        if not user.is_active:
            raise ServiceError("账户已被禁用", status_code=403)
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
        if user_queued + request.batch_count > queue.max_queued_per_user:
            raise ServiceError(
                "当前账户排队图片已达到上限",
                code="queue_full",
                status_code=429,
            )
        if global_queued + request.batch_count > queue.max_queued_global:
            raise ServiceError(
                "系统排队图片已达到上限",
                code="queue_full",
                status_code=429,
            )

        reserved = money(channel.price_rmb * request.batch_count)
        self.billing.reserve(user, reserved)
        job = GenerationJob(
            user_id=user.id,
            workspace_id=workspace.id,
            channel_id=channel.identifier,
            channel_label=channel.label,
            channel_config_version=self.channels.version,
            mode=request.mode,
            prompt=request.prompt.strip(),
            model=selected_model.identifier,
            size=normalized_size,
            quality=request.quality,
            output_format=request.output_format,
            compression=request.compression,
            transparent_background=request.transparent_background,
            requested_count=request.batch_count,
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
            raise ServiceError(
                "当前工作站已有图片任务，请等待完成或先取消",
                code="workspace_generation_active",
                status_code=409,
            ) from exc
        for position, asset in enumerate(references):
            db.session.add(
                GenerationReference(
                    job_id=job.id,
                    asset_id=asset.id,
                    position=position,
                )
            )
        for position in range(request.batch_count):
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
            }
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
        owner_query = select(GenerationJob.user_id).where(GenerationJob.id == job_id)
        if not admin:
            owner_query = owner_query.where(GenerationJob.user_id == user_id)
        owner_id = db.session.scalar(owner_query)
        if owner_id is None:
            raise ServiceError("生成任务不存在", status_code=404)
        user = self.billing.lock_user(owner_id)
        query = (
            select(GenerationJob)
            .options(selectinload(GenerationJob.items))
            .where(GenerationJob.id == job_id)
        )
        if not admin:
            query = query.where(GenerationJob.user_id == user_id)
        job = db.session.scalar(query.with_for_update())
        if job is None:
            raise ServiceError("生成任务不存在", status_code=404)
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
        query = select(GenerationJob).options(
            selectinload(GenerationJob.items),
            selectinload(GenerationJob.references).selectinload(GenerationReference.asset),
            selectinload(GenerationJob.user),
        )
        if not admin:
            query = query.where(GenerationJob.user_id == user_id)
        if workspace_id:
            query = query.where(GenerationJob.workspace_id == workspace_id)
        cutoff = utcnow() - timedelta(days=self.channels.queue.history_retention_days)
        query = query.where(
            (GenerationJob.completed_at.is_(None)) | (GenerationJob.completed_at >= cutoff)
        )
        query = query.order_by(GenerationJob.created_at.desc()).limit(min(max(limit, 1), 200))
        return list(db.session.scalars(query))

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
        exact = self._duration_samples(job, exact=True)
        samples = exact if len(exact) >= 3 else self._duration_samples(job, exact=False)
        if not samples:
            return Decimal(channel.limits.estimated_seconds)
        samples.sort()
        index = max(0, math.ceil(len(samples) * 0.75) - 1)
        return Decimal(str(round(samples[index], 3)))

    def _duration_samples(self, job: GenerationJob, *, exact: bool) -> list[float]:
        query = (
            select(GenerationItem.elapsed_seconds)
            .join(GenerationJob)
            .where(
                GenerationItem.status == "succeeded",
                GenerationItem.elapsed_seconds.is_not(None),
                GenerationJob.channel_id == job.channel_id,
                GenerationJob.mode == job.mode,
            )
            .order_by(GenerationItem.completed_at.desc())
            .limit(50)
        )
        if exact:
            query = query.where(
                GenerationJob.size == job.size,
                GenerationJob.quality == job.quality,
            )
        return [float(value) for value in db.session.scalars(query) if value and value > 0]

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
        job.completed_at = max(
            (item.completed_at for item in job.items if item.completed_at),
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

    @staticmethod
    def _validate_request(channel: Channel, request: SubmitGeneration) -> str:
        if request.mode not in channel.capabilities.modes:
            raise ServiceError(f"{channel.label} 不支持当前生成模式")
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > 8000:
            raise ServiceError("提示词长度必须在 1 到 8000 个字符之间")
        normalized_size = normalize_image_size(request.size)
        if request.quality not in channel.capabilities.qualities:
            raise ServiceError(f"{channel.label} 不支持质量 {request.quality}")
        if request.output_format not in channel.capabilities.formats:
            raise ServiceError(f"{channel.label} 不支持格式 {request.output_format}")
        if request.transparent_background and request.output_format not in {"png", "webp"}:
            raise ServiceError("透明背景仅支持 PNG 或 WebP 格式")
        if not 0 <= request.compression <= 100:
            raise ServiceError("压缩质量必须在 0 到 100 之间")
        if not 1 <= request.batch_count <= 20:
            raise ServiceError("单批生成张数必须在 1 到 20 之间")
        return normalized_size

    @staticmethod
    def _validate_references(channel: Channel, mode: str, references: list[Asset]) -> None:
        if mode == "img2img" and not references:
            raise ServiceError("垫图生图至少需要一张垫图")
        if mode == "text2img" and references:
            raise ServiceError("文生图任务不能携带垫图")
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
