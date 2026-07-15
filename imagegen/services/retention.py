from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from ..config.channels import ChannelRegistry
from ..extensions import db
from ..models import (
    Asset,
    ConversationAttachment,
    GenerationJob,
    GenerationReference,
    utcnow,
)
from ..storage import ImageStorage

LOGGER = logging.getLogger(__name__)
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "canceled", "partial")


class RetentionService:
    def __init__(self, storage: ImageStorage, channels: ChannelRegistry):
        self.storage = storage
        self.channels = channels

    def cleanup(self) -> dict[str, int]:
        cutoff = utcnow() - timedelta(days=self.channels.queue.history_retention_days)
        jobs = list(
            db.session.execute(
                select(
                    GenerationJob.id,
                    GenerationJob.user_id,
                    GenerationJob.workspace_id,
                ).where(
                    GenerationJob.completed_at.is_not(None),
                    GenerationJob.completed_at < cutoff,
                    GenerationJob.status.in_(TERMINAL_JOB_STATUSES),
                )
            )
        )
        deleted_jobs = 0
        errors = 0
        for job_id, user_id, workspace_id in jobs:
            try:
                job = db.session.scalar(
                    select(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.completed_at.is_not(None),
                        GenerationJob.completed_at < cutoff,
                        GenerationJob.status.in_(TERMINAL_JOB_STATUSES),
                    )
                    .with_for_update()
                )
                if job is None:
                    db.session.commit()
                    continue
                self.storage.delete_job_directory(user_id, workspace_id, job_id)
                db.session.delete(job)
                db.session.commit()
                deleted_jobs += 1
            except Exception:
                db.session.rollback()
                errors += 1
                LOGGER.warning("清理历史生成任务失败：%s", job_id, exc_info=True)

        orphaned_assets = list(
            db.session.execute(
                select(Asset.id, Asset.storage_path).where(
                    Asset.deleted_at.is_not(None),
                    ~select(GenerationReference.asset_id)
                    .where(GenerationReference.asset_id == Asset.id)
                    .exists(),
                    ~select(ConversationAttachment.asset_id)
                    .where(ConversationAttachment.asset_id == Asset.id)
                    .exists(),
                )
            )
        )
        deleted_assets = 0
        for asset_id, storage_path in orphaned_assets:
            try:
                self.storage.delete(storage_path)
                asset = db.session.get(Asset, asset_id)
                if asset is not None:
                    db.session.delete(asset)
                db.session.commit()
                deleted_assets += 1
            except Exception:
                db.session.rollback()
                errors += 1
                LOGGER.warning("清理孤立素材失败：%s", asset_id, exc_info=True)
        return {"jobs": deleted_jobs, "assets": deleted_assets, "errors": errors}
