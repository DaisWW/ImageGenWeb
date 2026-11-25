from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict

from sqlalchemy import and_, select
from sqlalchemy.orm import defer, joinedload, load_only

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, GenerationApiKey, GenerationItem, GenerationJob, User, Workspace
from .billing import BillingService
from .generations import GenerationService, SubmitGeneration


class GenerationApiService:
    def __init__(self, billing: BillingService, generations: GenerationService):
        self.billing = billing
        self.generations = generations

    def get_key(self, user_id: int) -> GenerationApiKey | None:
        return db.session.get(GenerationApiKey, user_id)

    def issue_key(self, user_id: int, workspace_id: str) -> str:
        self.billing.lock_user(user_id)
        workspace = db.session.scalar(
            select(Workspace)
            .options(load_only(Workspace.name, Workspace.kind))
            .where(
                Workspace.id == workspace_id,
                Workspace.user_id == user_id,
                Workspace.kind == "image",
            )
        )
        if workspace is None:
            raise ServiceError("工作站不存在", code="workspace_not_found", status_code=404)

        token = f"snow_{secrets.token_urlsafe(32)}"
        key = self.get_key(user_id)
        action = "generation_api.key.rotate" if key else "generation_api.key.enable"
        if key is None:
            key = GenerationApiKey(user_id=user_id)
            db.session.add(key)
        key.workspace = workspace
        key.token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        db.session.add(
            AuditLog(
                actor_user_id=user_id,
                action=action,
                target_type="workspace",
                target_id=workspace.id,
            )
        )
        db.session.commit()
        return token

    def revoke_key(self, user_id: int) -> None:
        self.billing.lock_user(user_id)
        key = self.get_key(user_id)
        if key is not None:
            db.session.add(
                AuditLog(
                    actor_user_id=user_id,
                    action="generation_api.key.disable",
                    target_type="workspace",
                    target_id=key.workspace_id,
                )
            )
            db.session.delete(key)
        db.session.commit()

    def authenticate(self, token: str) -> GenerationApiKey:
        if not re.fullmatch(r"snow_[A-Za-z0-9_-]{43}", token):
            raise self._invalid_key()
        key = db.session.scalar(
            select(GenerationApiKey)
            .join(User)
            .options(
                joinedload(GenerationApiKey.workspace, innerjoin=True).load_only(
                    Workspace.name, Workspace.kind
                )
            )
            .where(
                GenerationApiKey.token_digest == hashlib.sha256(token.encode("ascii")).hexdigest(),
                User.status == "active",
            )
        )
        if key is None:
            raise self._invalid_key()
        return key

    def submit(
        self, key: GenerationApiKey, request_id: str, request: SubmitGeneration
    ) -> tuple[GenerationJob, bool]:
        digest = hashlib.sha256(
            json.dumps(asdict(request), sort_keys=True).encode("ascii")
        ).hexdigest()
        # Serialize submission, reservation and key changes on the account row.
        user = self.billing.lock_user(key.user_id)
        active_key = db.session.scalar(
            select(GenerationApiKey.user_id).where(
                GenerationApiKey.user_id == user.id,
                GenerationApiKey.token_digest == key.token_digest,
            )
        )
        if not user.is_active or active_key is None:
            raise self._invalid_key()

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

    @staticmethod
    def _invalid_key() -> ServiceError:
        return ServiceError("生图 API 密钥无效", code="invalid_api_key", status_code=401)
