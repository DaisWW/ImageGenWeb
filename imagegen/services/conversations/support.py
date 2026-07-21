from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ...config.chat_models import ChatModelConfig, ChatModelRegistry
from ...errors import ServiceError
from ...extensions import db
from ...integrations.openai_chat import ChatCompletion, OpenAIChatClient, OpenAIChatError
from ...models import (
    Asset,
    ConversationAttachment,
    ConversationMessage,
    GenerationJob,
    Workspace,
    new_public_id,
)
from ...storage import ImageStorage
from ..creative import CASE_CATALOG, CREATIVE_ROUTER
from ..creative.models import CreativeCase, PromptTemplate
from ..runtime_logs import RuntimeLogService
from ..settings import SystemSettingsService
from .context import ConversationContextManager


@dataclass(slots=True)
class ConversationDependencies:
    chat_models: ChatModelRegistry
    storage: ImageStorage
    settings: SystemSettingsService
    runtime_logs: RuntimeLogService
    context: ConversationContextManager
    client: OpenAIChatClient


class ConversationSupport:
    def __init__(self, dependencies: ConversationDependencies):
        self.dependencies = dependencies

    @property
    def chat_models(self) -> ChatModelRegistry:
        return self.dependencies.chat_models

    @property
    def storage(self) -> ImageStorage:
        return self.dependencies.storage

    @property
    def settings(self) -> SystemSettingsService:
        return self.dependencies.settings

    @property
    def runtime_logs(self) -> RuntimeLogService:
        return self.dependencies.runtime_logs

    @property
    def context(self) -> ConversationContextManager:
        return self.dependencies.context

    @property
    def client(self) -> OpenAIChatClient:
        return self.dependencies.client

    @staticmethod
    def _creative_query(workspace: Workspace) -> str:
        messages = list(
            db.session.scalars(
                select(ConversationMessage.content)
                .where(
                    ConversationMessage.workspace_id == workspace.id,
                    ConversationMessage.role == "user",
                )
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                .limit(6)
            )
        )
        return "\n".join(reversed(messages))

    def _creative_matches(
        self,
        workspace: Workspace,
        *,
        direction_id: str,
    ) -> tuple[tuple[PromptTemplate, ...], tuple[CreativeCase, ...]]:
        query = self._creative_query(workspace)
        templates = CREATIVE_ROUTER.route(query, direction_id=direction_id)
        cases = CASE_CATALOG.search(
            query,
            direction_id=direction_id,
            templates=templates,
        )
        return templates, cases

    @staticmethod
    def _draft_references(draft: dict[str, Any], candidates: list[Asset]) -> list[Asset]:
        by_id = {asset.id: asset for asset in candidates}
        return [
            by_id[asset_id]
            for asset_id in (str(item) for item in draft.get("reference_ids", []))
            if asset_id in by_id
        ]

    @staticmethod
    def _message_id(value: str) -> str:
        message_id = str(value).strip().lower()
        if len(message_id) != 32 or any(
            character not in "0123456789abcdef" for character in message_id
        ):
            raise ServiceError("消息 ID 无效")
        return message_id

    def _model(self, model_id: str) -> ChatModelConfig:
        try:
            return self.chat_models.get(model_id)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc

    def _load_assets(self, workspace: Workspace, asset_ids: tuple[str, ...]) -> list[Asset]:
        runtime = self.settings.runtime()
        if len(asset_ids) != len(set(asset_ids)):
            raise ServiceError("参考图不能重复")
        if len(asset_ids) > runtime.max_chat_attachments:
            raise ServiceError(f"单条消息最多附加 {runtime.max_chat_attachments} 张参考图")
        if not asset_ids:
            return []
        assets = list(
            db.session.scalars(
                select(Asset).where(
                    Asset.workspace_id == workspace.id,
                    Asset.id.in_(asset_ids),
                    Asset.deleted_at.is_(None),
                )
            )
        )
        by_id = {asset.id: asset for asset in assets}
        if any(asset_id not in by_id for asset_id in asset_ids):
            raise ServiceError("选择的参考图不存在")
        ordered = [by_id[asset_id] for asset_id in asset_ids]
        if any(asset.byte_count > runtime.max_attachment_bytes for asset in ordered):
            raise ServiceError(f"单张参考图不能超过 {runtime.max_attachment_mb} MiB")
        if sum(asset.byte_count for asset in ordered) > runtime.max_attachment_total_bytes:
            raise ServiceError(f"参考图合计不能超过 {runtime.max_attachment_total_mb} MiB")
        return ordered

    def _merge_context_assets(self, workspace: Workspace, assets: list[Asset]) -> list[Asset]:
        ordered = list({asset.id: asset for asset in assets}.values())
        runtime = self.settings.runtime()
        if len(ordered) > runtime.max_chat_attachments:
            raise ServiceError(f"单条消息最多附加 {runtime.max_chat_attachments} 张参考图")
        if any(asset.byte_count > runtime.max_attachment_bytes for asset in ordered):
            raise ServiceError(f"单张参考图不能超过 {runtime.max_attachment_mb} MiB")
        if sum(asset.byte_count for asset in ordered) > runtime.max_attachment_total_bytes:
            raise ServiceError(f"参考图合计不能超过 {runtime.max_attachment_total_mb} MiB")
        return ordered

    def _user_model_message(self, content: str, attachments: list[Asset]) -> dict[str, Any]:
        if not attachments:
            return {"role": "user", "content": content}
        parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
        for asset in attachments:
            encoded = base64.b64encode(self.storage.read_bytes(asset.storage_path)).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{asset.mime_type};base64,{encoded}"},
                }
            )
        return {"role": "user", "content": parts}

    @staticmethod
    def _assistant_message(
        workspace: Workspace,
        model: ChatModelConfig,
        result: ChatCompletion,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> ConversationMessage:
        return ConversationMessage(
            id=new_public_id(),
            workspace_id=workspace.id,
            role="assistant",
            kind=kind,
            content=result.content,
            payload=payload,
            provider_id=model.identifier,
            provider_label=model.label,
            model=model.model,
            upstream_request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            elapsed_seconds=(
                None if result.elapsed_seconds is None else round(result.elapsed_seconds, 3)
            ),
        )

    @staticmethod
    def _attach(message: ConversationMessage, assets: list[Asset]) -> None:
        message.attachments = [
            ConversationAttachment(asset=asset, position=position)
            for position, asset in enumerate(assets)
        ]

    def _validate_message(self, content: str, *, has_attachments: bool) -> str:
        content = content.strip()
        if not content and has_attachments:
            content = "请分析这些参考图，帮助我明确生图需求。"
        if not content:
            raise ServiceError("请输入消息")
        maximum = self.settings.runtime().max_message_characters
        if len(content) > maximum:
            raise ServiceError(f"单条消息不能超过 {maximum} 个字符")
        return content

    @staticmethod
    def _remember_preferences(
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool | None = None,
        creative_direction_id: str | None = None,
    ) -> None:
        settings = dict(workspace.settings or {})
        settings["chat_model_id"] = model_id
        if translate_to_english is not None:
            settings["translate_prompt"] = translate_to_english
        if creative_direction_id is not None:
            settings["creative_direction_id"] = creative_direction_id
        workspace.settings = settings

    @staticmethod
    def _ensure_workspace_unlocked(workspace: Workspace) -> None:
        active = db.session.scalar(
            select(GenerationJob.id)
            .where(
                GenerationJob.workspace_id == workspace.id,
                GenerationJob.status.in_(["queued", "running", "canceling"]),
            )
            .limit(1)
        )
        if active:
            raise ServiceError(
                "当前图片尚未生成完成，请等待完成或先取消任务",
                code="workspace_generation_active",
                status_code=409,
            )

    def _record_chat_success(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        event: str,
        result: ChatCompletion,
        *,
        details: dict[str, Any],
    ) -> None:
        self.runtime_logs.record(
            category="chat",
            event=event,
            status="success",
            message="对话模型调用成功",
            source="web",
            user_id=workspace.user_id,
            user_label=workspace.user.display_name or workspace.user.username,
            workspace_id=workspace.id,
            workspace_label=workspace.name,
            provider_id=model.identifier,
            provider_label=model.label,
            model=model.model,
            upstream_request_id=result.request_id,
            elapsed_seconds=result.elapsed_seconds,
            details={
                **details,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )

    @staticmethod
    def _structured_output_error(
        error: ServiceError,
        result: ChatCompletion,
    ) -> OpenAIChatError:
        return OpenAIChatError(
            str(error),
            code="chat_invalid_response",
            request_id=result.request_id,
            elapsed_seconds=result.elapsed_seconds,
            details={"validation": "structured_output_contract"},
        )

    def _raise_chat_error(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        event: str,
        error: OpenAIChatError,
    ) -> None:
        error_id = self._record_chat_error(workspace, model, event, error)
        raise ServiceError(
            str(error),
            code=error.code,
            status_code=error.status_code,
            error_id=error_id,
        ) from error

    def _record_chat_error(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        event: str,
        error: OpenAIChatError,
    ) -> str:
        db.session.rollback()
        entry = self.runtime_logs.commit_best_effort(
            category="chat",
            event=event,
            status="error",
            message="对话模型调用失败",
            source="web",
            user_id=workspace.user_id,
            user_label=workspace.user.display_name or workspace.user.username,
            workspace_id=workspace.id,
            workspace_label=workspace.name,
            provider_id=model.identifier,
            provider_label=model.label,
            model=model.model,
            error_code=error.code,
            http_status=error.upstream_status,
            upstream_request_id=error.request_id,
            elapsed_seconds=error.elapsed_seconds,
            details={"diagnostics": error.details},
        )
        return entry.id if entry is not None else ""
