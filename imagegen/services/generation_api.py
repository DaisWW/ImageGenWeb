from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.orm import defer, joinedload

from ..errors import ServiceError
from ..extensions import db
from ..models import (
    GENERATION_API_WORKSPACE_KIND,
    GenerationApiKey,
    GenerationItem,
    GenerationJob,
    Workspace,
    new_public_id,
)
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

        existing = _read_job(
            GenerationJob.user_id == user.id,
            GenerationJob.api_request_id == request_id,
        )
        if existing is not None:
            if existing.api_request_digest == digest:
                return existing, False
            raise ServiceError(
                "此请求 ID 已用于其他生成参数", code="idempotency_conflict", status_code=409
            )

        try:
            job = self.generations.submit(
                user.id,
                _get_or_create_api_workspace(user.id),
                request,
                api_request_id=request_id,
                api_request_digest=digest,
            )
        except Exception:
            db.session.rollback()
            raise
        return job, True

    def get_job(self, key: GenerationApiKey, job_id: str) -> GenerationJob:
        job = _read_job(*self._job_scope(key, job_id))
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
    def _job_scope(key: GenerationApiKey, job_id: str) -> tuple[ColumnElement[bool], ...]:
        return (
            GenerationJob.id == job_id,
            GenerationJob.user_id == key.user_id,
            GenerationJob.api_request_id.is_not(None),
        )


def _get_or_create_api_workspace(user_id: int) -> Workspace:
    # The caller holds the account lock until the workspace and job are committed.
    workspace = db.session.scalar(
        select(Workspace).where(
            Workspace.user_id == user_id, Workspace.kind == GENERATION_API_WORKSPACE_KIND
        )
    )
    if workspace is None:
        workspace_id = new_public_id()
        workspace = Workspace(
            id=workspace_id,
            user_id=user_id,
            name=f"生图 API {workspace_id}",
            kind=GENERATION_API_WORKSPACE_KIND,
        )
        db.session.add(workspace)
    return workspace


def _read_job(*conditions: ColumnElement[bool]) -> GenerationJob | None:
    query = (
        select(GenerationJob)
        .options(
            defer(GenerationJob.prompt),
            joinedload(GenerationJob.items).defer(GenerationItem.prompt),
        )
        .where(*conditions)
    )
    return db.session.execute(query).unique().scalar_one_or_none()
