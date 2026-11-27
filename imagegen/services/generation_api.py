from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, replace

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
from .generations import GenerationMaskInput, GenerationService, SubmitGeneration
from .workspaces import WorkspaceService

LOGGER = logging.getLogger(__name__)


class GenerationApiService:
    def __init__(
        self,
        keys: GenerationApiKeyService,
        generations: GenerationService,
        workspaces: WorkspaceService,
    ):
        self.keys = keys
        self.generations = generations
        self.workspaces = workspaces

    def submit(
        self,
        key: GenerationApiKey,
        request_id: str,
        request: SubmitGeneration,
        *,
        reference_uploads: tuple[tuple[str, bytes], ...] = (),
        mask_content: bytes | None = None,
    ) -> tuple[GenerationJob, bool]:
        if (
            mask_content is not None
            and request.workflow.get("generation_strategy", "sample") != "sample"
        ):
            raise ServiceError(
                "局部重绘当前仅支持同提示词抽样",
                code="mask_strategy_invalid",
                status_code=422,
            )
        digest = _request_digest(request, reference_uploads, mask_content)
        user = self.keys.lock_active_owner(key)

        existing = _read_job(
            GenerationJob.user_id == user.id,
            GenerationJob.api_request_id == request_id,
        )
        if existing is not None:
            if existing.api_request_digest == digest or (
                not reference_uploads
                and mask_content is None
                and _matches_legacy_digest(request, existing.api_request_digest)
            ):
                return existing, False
            raise ServiceError(
                "此请求 ID 已用于其他生成参数", code="idempotency_conflict", status_code=409
            )

        if mask_content is not None and len(reference_uploads) != 1:
            raise ServiceError(
                "局部重绘必须且只能上传一张原图",
                code="mask_reference_count_invalid",
                status_code=422,
            )

        user_id = user.id
        workspace = _new_api_workspace(user_id)
        try:
            db.session.add(workspace)
            assets = (
                self.workspaces.add_assets(workspace, reference_uploads, commit=False)
                if reference_uploads
                else []
            )
            generation = replace(
                request,
                reference_ids=tuple(asset.id for asset in assets),
                mask=(
                    GenerationMaskInput(target_asset_id=assets[0].id, content=mask_content)
                    if mask_content is not None
                    else None
                ),
            )
            job = self.generations.submit(
                user_id,
                workspace,
                generation,
                api_request_id=request_id,
                api_request_digest=digest,
            )
        except Exception:
            db.session.rollback()
            try:
                self.workspaces.storage.delete_workspace(user_id, workspace.id)
            except Exception:
                LOGGER.warning("清理未提交的生图 API 工作站失败：%s", workspace.id, exc_info=True)
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


def _new_api_workspace(user_id: int) -> Workspace:
    workspace_id = new_public_id()
    return Workspace(
        id=workspace_id,
        user_id=user_id,
        name=f"生图 API {workspace_id}",
        kind=GENERATION_API_WORKSPACE_KIND,
        settings={},
    )


def _request_digest(
    request: SubmitGeneration,
    reference_uploads: tuple[tuple[str, bytes], ...] = (),
    mask_content: bytes | None = None,
) -> str:
    payload = asdict(request)
    if reference_uploads:
        payload["reference_uploads"] = [
            {"name": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in reference_uploads
        ]
    if mask_content is not None:
        payload["mask_content_sha256"] = hashlib.sha256(mask_content).hexdigest()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("ascii")).hexdigest()


def _matches_legacy_digest(request: SubmitGeneration, digest: str | None) -> bool:
    # Before workflow options were exposed, JSON requests hashed an empty workflow.
    return (
        request.workflow.get("generation_stage", "final") == "final"
        and request.workflow.get("generation_strategy", "sample") == "sample"
        and request.workflow.get("creative_direction_id", "auto") == "auto"
        and _request_digest(replace(request, workflow={})) == digest
    )


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
