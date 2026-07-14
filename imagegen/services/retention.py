from __future__ import annotations

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


class RetentionService:
    def __init__(self, storage: ImageStorage, channels: ChannelRegistry):
        self.storage = storage
        self.channels = channels

    def cleanup(self) -> dict[str, int]:
        cutoff = utcnow() - timedelta(days=self.channels.queue.history_retention_days)
        jobs = list(
            db.session.scalars(
                select(GenerationJob).where(
                    GenerationJob.completed_at.is_not(None),
                    GenerationJob.completed_at < cutoff,
                )
            )
        )
        directories = [(job.user_id, job.workspace_id, job.id) for job in jobs]
        for job in jobs:
            db.session.delete(job)
        db.session.commit()
        for user_id, workspace_id, job_id in directories:
            self.storage.delete_job_directory(user_id, workspace_id, job_id)

        orphaned_assets = list(
            db.session.scalars(
                select(Asset).where(
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
        for asset in orphaned_assets:
            self.storage.delete(asset.storage_path)
            db.session.delete(asset)
        db.session.commit()
        return {"jobs": len(jobs), "assets": len(orphaned_assets)}
