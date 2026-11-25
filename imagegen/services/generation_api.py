from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from sqlalchemy import and_, select
from sqlalchemy.orm import defer, joinedload

from ..errors import ServiceError
from ..extensions import db
from ..models import GenerationApiKey, GenerationItem, GenerationJob
from .generation_api_keys import GenerationApiKeyService
from .generations import GenerationService, SubmitGeneration


class GenerationApiService:
    def __init__(self, keys: GenerationApiKeyService, generations: GenerationService):
        self.keys = keys
        self.generations = generations

    def submit(
        self, key: GenerationApiKey, request_id: str, request: SubmitGeneration
    ) -> tuple[GenerationJob, bool]:
        digest = hashlib.sha256(
            json.dumps(asdict(request), sort_keys=True).encode("ascii")
        ).hexdigest()
        user = self.keys.lock_active_owner(key)

        existing = db.session.execute(
            select(
                GenerationJob.id, GenerationJob.workspace_id, GenerationJob.api_request_digest
            ).where(
                GenerationJob.user_id == user.id,
                GenerationJob.api_request_id == request_id,
            )
        ).one_or_none()
        if existing is not None:
            if existing.workspace_id != key.workspace_id:
                raise ServiceError(
                    "此请求 ID 已在另一个工作站提交",
                    code="idempotency_conflict",
                    status_code=409,
                )
            if existing.api_request_digest != digest:
                raise ServiceError(
                    "此请求 ID 已用于其他生成参数",
                    code="idempotency_conflict",
                    status_code=409,
                )
            return self.get_job(key, existing.id), False

        job = self.generations.submit(
            user.id,
            key.workspace,
            request,
            api_request_id=request_id,
            api_request_digest=digest,
        )
        return self.get_job(key, job.id), True

    def get_job(self, key: GenerationApiKey, job_id: str) -> GenerationJob:
        job = (
            db.session.execute(
                select(GenerationJob)
                .options(
                    defer(GenerationJob.prompt),
                    joinedload(GenerationJob.items).defer(GenerationItem.prompt),
                )
                .where(*self._job_scope(key, job_id))
            )
            .unique()
            .scalar_one_or_none()
        )
        if job is None:
            raise ServiceError("生成任务不存在", code="generation_not_found", status_code=404)
        return job

    def get_image(self, key: GenerationApiKey, job_id: str, item_id: str) -> tuple[str, str | None]:
        image = db.session.execute(
            select(GenerationItem.output_path, GenerationItem.output_mime_type)
            .select_from(GenerationJob)
            .outerjoin(
                GenerationItem,
                and_(
                    GenerationItem.job_id == GenerationJob.id,
                    GenerationItem.id == item_id,
                    GenerationItem.status == "succeeded",
                ),
            )
            .where(*self._job_scope(key, job_id))
        ).one_or_none()
        if image is None:
            raise ServiceError("生成任务不存在", code="generation_not_found", status_code=404)
        if not image.output_path:
            raise ServiceError("图片不存在", code="image_not_found", status_code=404)
        return image.output_path, image.output_mime_type

    @staticmethod
    def _job_scope(key: GenerationApiKey, job_id: str):
        return (
            GenerationJob.id == job_id,
            GenerationJob.user_id == key.user_id,
            GenerationJob.workspace_id == key.workspace_id,
            GenerationJob.api_request_id.is_not(None),
        )
