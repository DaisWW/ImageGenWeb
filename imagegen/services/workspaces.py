from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..errors import ServiceError
from ..extensions import db
from ..models import (
    Asset,
    ConversationAttachment,
    ConversationMessage,
    ConversationState,
    GenerationItem,
    GenerationJob,
    GenerationReference,
    Workspace,
    new_public_id,
    utcnow,
)
from ..storage import ImageStorage
from .billing import BillingService
from .common import money
from .settings import SystemSettingsService
from .starter_content import (
    REFERENCE_STARTER_ASSET_NAME,
    REFERENCE_STARTER_NAME,
    REFERENCE_STARTER_PROMPT,
    REFERENCE_STARTER_SUMMARY,
    TEXT_STARTER_NAME,
    TEXT_STARTER_PROMPT,
    TEXT_STARTER_SUMMARY,
)
from .workspace_settings import (
    default_workspace_settings,
    sanitize_workspace_settings,
)

WORKSPACE_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


class WorkspaceService:
    def __init__(
        self,
        storage: ImageStorage,
        billing: BillingService,
        starter_reference_path: str | Path,
        settings: SystemSettingsService,
    ):
        self.storage = storage
        self.billing = billing
        self.starter_reference_path = Path(starter_reference_path)
        self.settings = settings

    @property
    def max_workspaces(self) -> int:
        return self.settings.runtime().max_workspaces_per_user

    def list(self, user_id: int) -> list[Workspace]:
        return list(
            db.session.scalars(
                select(Workspace)
                .options(selectinload(Workspace.assets))
                .where(Workspace.user_id == user_id)
                .order_by(Workspace.position.asc(), Workspace.updated_at.desc())
            )
        )

    def create(self, user_id: int, name: str, kind: str = "image") -> Workspace:
        self.billing.lock_user(user_id)
        count = (
            db.session.scalar(select(func.count(Workspace.id)).where(Workspace.user_id == user_id))
            or 0
        )
        max_workspaces = self.max_workspaces
        if count >= max_workspaces:
            raise ServiceError(
                f"每个用户最多创建 {max_workspaces} 个工作站",
                code="workspace_limit",
                status_code=409,
            )
        name = self._validate_name(name.strip() or self._next_workspace_name(user_id))
        kind = str(kind).strip().lower()
        if kind not in {"image", "animation"}:
            raise ServiceError("工作站类型无效")
        self._ensure_name_available(user_id, name)
        minimum_position = db.session.scalar(
            select(func.min(Workspace.position)).where(Workspace.user_id == user_id)
        )
        workspace = Workspace(
            user_id=user_id,
            name=name,
            kind=kind,
            position=0 if minimum_position is None else minimum_position - 1,
            settings=default_workspace_settings(kind),
        )
        db.session.add(workspace)
        self._commit_name_change()
        return workspace

    def ensure_starter_workspaces(self, user_id: int) -> list[Workspace]:
        existing = self.list(user_id)
        if existing:
            return existing
        if not self.settings.runtime().create_starter_workspaces:
            return []

        self.billing.lock_user(user_id)
        if db.session.scalar(select(Workspace.id).where(Workspace.user_id == user_id).limit(1)):
            return self.list(user_id)

        reference_content = self.starter_reference_path.read_bytes()
        reference_time = utcnow()
        text_time = reference_time + timedelta(microseconds=1)
        reference_workspace = Workspace(
            id=new_public_id(),
            user_id=user_id,
            name=REFERENCE_STARTER_NAME,
            kind="image",
            position=1,
            settings=default_workspace_settings(),
            created_at=reference_time,
            updated_at=reference_time,
        )
        text_workspace = Workspace(
            id=new_public_id(),
            user_id=user_id,
            name=TEXT_STARTER_NAME,
            kind="image",
            position=0,
            settings=default_workspace_settings(),
            created_at=text_time,
            updated_at=text_time,
        )
        stored_paths: list[str] = []
        try:
            asset = self._reference_asset(
                reference_workspace,
                REFERENCE_STARTER_ASSET_NAME,
                reference_content,
                position=0,
            )
            stored_paths.append(asset.storage_path)
            text_draft = self._starter_draft(
                text_workspace,
                summary=TEXT_STARTER_SUMMARY,
                prompt=TEXT_STARTER_PROMPT,
            )
            reference_draft = self._starter_draft(
                reference_workspace,
                summary=REFERENCE_STARTER_SUMMARY,
                prompt=REFERENCE_STARTER_PROMPT,
                asset=asset,
            )
            db.session.add_all(
                [
                    reference_workspace,
                    text_workspace,
                    asset,
                    text_draft,
                    reference_draft,
                ]
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            for path in stored_paths:
                self.storage.delete(path)
            raise
        return self.list(user_id)

    def update(self, workspace: Workspace, payload: dict[str, Any]) -> Workspace:
        if "name" in payload:
            name = self._validate_name(str(payload["name"]))
            self._ensure_name_available(workspace.user_id, name, exclude_id=workspace.id)
            workspace.name = name
        if "settings" in payload:
            if not isinstance(payload["settings"], dict):
                raise ServiceError("工作站参数格式无效")
            workspace.settings = sanitize_workspace_settings(
                {**(workspace.settings or {}), **payload["settings"]},
                self.settings.runtime(),
            )
        if "name" in payload:
            self._commit_name_change()
        else:
            db.session.commit()
        return workspace

    def reorder(self, user_id: int, workspace_ids: list[str]) -> None:
        if len(workspace_ids) != len(set(workspace_ids)):
            raise ServiceError("工作站排序数据不能包含重复项")
        self.billing.lock_user(user_id)
        workspaces = list(db.session.scalars(select(Workspace).where(Workspace.user_id == user_id)))
        by_id = {workspace.id: workspace for workspace in workspaces}
        if len(workspace_ids) != len(workspaces) or set(workspace_ids) != set(by_id):
            raise ServiceError("工作站排序数据必须包含当前账户的全部工作站")
        ordered = [by_id[workspace_id] for workspace_id in workspace_ids]
        for position, workspace in enumerate(ordered):
            workspace.position = position
        db.session.commit()

    def add_assets(self, workspace: Workspace, uploads: Iterable[tuple[str, bytes]]) -> list[Asset]:
        uploads = list(uploads)
        if not uploads:
            raise ServiceError("请选择参考图")
        runtime = self.settings.runtime()
        if len(uploads) > runtime.max_assets_per_workspace:
            raise ServiceError(f"单次最多上传 {runtime.max_assets_per_workspace} 张参考图")
        if any(len(content) > runtime.max_attachment_bytes for _name, content in uploads):
            raise ServiceError(f"单张参考图不能超过 {runtime.max_attachment_mb} MiB")
        if sum(len(content) for _name, content in uploads) > runtime.max_attachment_total_bytes:
            raise ServiceError(f"参考图合计不能超过 {runtime.max_attachment_total_mb} MiB")
        db.session.scalar(
            select(Workspace.id).where(Workspace.id == workspace.id).with_for_update()
        )
        active_count = (
            db.session.scalar(
                select(func.count(Asset.id)).where(
                    Asset.workspace_id == workspace.id,
                    Asset.deleted_at.is_(None),
                )
            )
            or 0
        )
        if active_count + len(uploads) > runtime.max_assets_per_workspace:
            raise ServiceError(f"每个工作站最多保留 {runtime.max_assets_per_workspace} 张参考图")
        next_position = (
            db.session.scalar(
                select(func.max(Asset.position)).where(Asset.workspace_id == workspace.id)
            )
            or -1
        ) + 1
        saved_paths: list[str] = []
        assets: list[Asset] = []
        try:
            for offset, (original_name, content) in enumerate(uploads):
                asset = self._reference_asset(
                    workspace,
                    original_name,
                    content,
                    position=next_position + offset,
                )
                saved_paths.append(asset.storage_path)
                db.session.add(asset)
                assets.append(asset)
            db.session.commit()
            return assets
        except Exception:
            db.session.rollback()
            for path in saved_paths:
                self.storage.delete(path)
            raise

    def reorder_assets(self, workspace: Workspace, asset_ids: list[str]) -> None:
        active_assets = list(
            db.session.scalars(
                select(Asset).where(
                    Asset.workspace_id == workspace.id,
                    Asset.deleted_at.is_(None),
                )
            )
        )
        by_id = {asset.id: asset for asset in active_assets}
        if set(asset_ids) != set(by_id) or len(asset_ids) != len(by_id):
            raise ServiceError("垫图排序数据无效")
        for position, asset_id in enumerate(asset_ids):
            by_id[asset_id].position = position
        db.session.commit()

    def remove_asset(self, workspace: Workspace, asset_id: str) -> None:
        asset = db.session.scalar(
            select(Asset).where(
                Asset.id == asset_id,
                Asset.workspace_id == workspace.id,
                Asset.deleted_at.is_(None),
            )
        )
        if asset is None:
            raise ServiceError("垫图不存在", status_code=404)
        generation_references = (
            db.session.scalar(
                select(func.count(GenerationReference.asset_id)).where(
                    GenerationReference.asset_id == asset.id
                )
            )
            or 0
        )
        conversation_references = (
            db.session.scalar(
                select(func.count(ConversationAttachment.asset_id)).where(
                    ConversationAttachment.asset_id == asset.id
                )
            )
            or 0
        )
        if generation_references or conversation_references:
            asset.deleted_at = utcnow()
        else:
            self.storage.delete(asset.storage_path)
            db.session.delete(asset)
        db.session.commit()

    def delete(self, workspace: Workspace) -> None:
        active = (
            db.session.scalar(
                select(func.count(GenerationItem.id))
                .join(GenerationJob)
                .where(
                    GenerationJob.workspace_id == workspace.id,
                    GenerationItem.status.in_(["running", "canceling"]),
                )
            )
            or 0
        )
        if active:
            raise ServiceError(
                "工作站仍有正在生成的任务，请先取消并等待结束",
                status_code=409,
            )
        user = self.billing.lock_user(workspace.user_id)
        queued_jobs = list(
            db.session.scalars(
                select(GenerationJob)
                .options(selectinload(GenerationJob.items))
                .where(GenerationJob.workspace_id == workspace.id)
            )
        )
        for job in queued_jobs:
            releasable = sum(
                (money(job.price_per_image_rmb) for item in job.items if item.status == "queued"),
                Decimal("0"),
            )
            self.billing.release(user, job, releasable)
        user_id, workspace_id = workspace.user_id, workspace.id
        db.session.delete(workspace)
        db.session.commit()
        self.storage.delete_workspace(user_id, workspace_id)

    def clear(self, workspace: Workspace) -> Workspace:
        active = (
            db.session.scalar(
                select(func.count(GenerationItem.id))
                .join(GenerationJob)
                .where(
                    GenerationJob.workspace_id == workspace.id,
                    GenerationItem.status.in_(["queued", "running", "canceling"]),
                )
            )
            or 0
        )
        if active:
            raise ServiceError(
                "当前帧动画尚未生成完成，请等待完成或先取消任务"
                if workspace.kind == "animation"
                else "当前图片尚未生成完成，请等待完成或先取消任务",
                code="workspace_generation_active",
                status_code=409,
            )
        jobs = list(
            db.session.scalars(
                select(GenerationJob).where(GenerationJob.workspace_id == workspace.id)
            )
        )
        messages = list(
            db.session.scalars(
                select(ConversationMessage).where(ConversationMessage.workspace_id == workspace.id)
            )
        )
        state = db.session.get(ConversationState, workspace.id)
        for record in [*jobs, *messages]:
            db.session.delete(record)
        if state:
            db.session.delete(state)
        db.session.flush()
        assets = list(db.session.scalars(select(Asset).where(Asset.workspace_id == workspace.id)))
        for asset in assets:
            db.session.delete(asset)
        settings = dict(workspace.settings or {})
        settings["prompt"] = ""
        workspace.settings = sanitize_workspace_settings(settings, self.settings.runtime())
        workspace.updated_at = utcnow()
        db.session.commit()
        self.storage.delete_workspace(workspace.user_id, workspace.id)
        return workspace

    def _reference_asset(
        self,
        workspace: Workspace,
        original_name: str,
        content: bytes,
        *,
        position: int,
    ) -> Asset:
        asset_id = new_public_id()
        stored = self.storage.save_reference(
            user_id=workspace.user_id,
            workspace_id=workspace.id,
            asset_id=asset_id,
            content=content,
        )
        return Asset(
            id=asset_id,
            workspace_id=workspace.id,
            original_name=(original_name or f"reference.{stored.extension}")[:255],
            storage_path=stored.relative_path,
            mime_type=stored.mime_type,
            byte_count=stored.byte_count,
            width=stored.width,
            height=stored.height,
            sha256=stored.sha256,
            position=position,
        )

    @staticmethod
    def _starter_draft(
        workspace: Workspace,
        *,
        summary: str,
        prompt: str,
        asset: Asset | None = None,
    ) -> ConversationMessage:
        reference_ids = [asset.id] if asset else []
        message = ConversationMessage(
            workspace_id=workspace.id,
            role="assistant",
            kind="prompt_draft",
            content=f"需求确认\n{summary}\n\n生图提示词\n{prompt}",
            payload={
                "summary_zh": summary,
                "prompt": prompt,
                "language": "zh",
                "reference_ids": reference_ids,
            },
            provider_label="创作示例",
        )
        if asset:
            message.attachments = [ConversationAttachment(asset=asset, position=0)]
        return message

    @staticmethod
    def _validate_name(name: str) -> str:
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ServiceError("工作站名称长度必须在 1 到 80 个字符之间")
        return name

    @staticmethod
    def _ensure_name_available(user_id: int, name: str, *, exclude_id: str = "") -> None:
        query = select(Workspace.id).where(
            Workspace.user_id == user_id,
            func.lower(Workspace.name) == name.lower(),
        )
        if exclude_id:
            query = query.where(Workspace.id != exclude_id)
        if db.session.scalar(query.limit(1)):
            raise WorkspaceService._name_conflict_error()

    @staticmethod
    def _commit_name_change() -> None:
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise WorkspaceService._name_conflict_error() from exc

    @staticmethod
    def _name_conflict_error() -> ServiceError:
        return ServiceError(
            "工作站名称已存在",
            code="workspace_name_exists",
            status_code=409,
        )

    @staticmethod
    def _next_workspace_name(user_id: int) -> str:
        names = set(db.session.scalars(select(Workspace.name).where(Workspace.user_id == user_id)))
        base = f"工作站-{utcnow().astimezone(WORKSPACE_TIMEZONE):%Y-%m-%d}"
        if base not in names:
            return base
        index = 2
        while f"{base} {index}" in names:
            index += 1
        return f"{base} {index}"
