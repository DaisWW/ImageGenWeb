from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config.chat_models import ChatModelConfig, ChatModelRegistry
from ..errors import ServiceError
from ..extensions import db
from ..integrations.openai_chat import ChatCompletion, OpenAIChatClient, OpenAIChatError
from ..models import (
    Asset,
    ConversationAttachment,
    ConversationMessage,
    ConversationState,
    GenerationJob,
    Workspace,
    utcnow,
)
from ..storage import ImageStorage
from .conversation_context import ConversationContextManager
from .conversation_prompts import (
    animation_runtime_prompt,
    chat_system_prompt,
    generation_mode_prompt,
)
from .prompt_drafts import PromptDraftParser
from .runtime_logs import RuntimeLogService
from .settings import SystemSettingsService


@dataclass(frozen=True)
class ConversationPage:
    messages: list[ConversationMessage]
    total: int
    has_more: bool


@dataclass(frozen=True)
class ConversationOperation:
    user_id: int
    kind: str
    label: str
    started_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "busy": True,
            "kind": self.kind,
            "label": self.label,
            "started_at": self.started_at.isoformat(),
        }


class ConversationService:
    def __init__(
        self,
        chat_models: ChatModelRegistry,
        storage: ImageStorage,
        settings: SystemSettingsService,
        runtime_logs: RuntimeLogService,
        client: OpenAIChatClient | None = None,
    ):
        self.chat_models = chat_models
        self.storage = storage
        self.settings = settings
        self.runtime_logs = runtime_logs
        self.client = client or OpenAIChatClient()
        self.context = ConversationContextManager(chat_models)
        self._operation_lock = Lock()
        self._operations: dict[str, ConversationOperation] = {}

    def list_messages(self, workspace: Workspace, *, limit: int = 200) -> ConversationPage:
        limit = min(500, max(1, limit))
        total = (
            db.session.scalar(
                select(func.count(ConversationMessage.id)).where(
                    ConversationMessage.workspace_id == workspace.id
                )
            )
            or 0
        )
        newest = list(
            db.session.scalars(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(ConversationMessage.workspace_id == workspace.id)
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                .limit(limit)
            )
        )
        return ConversationPage(
            messages=list(reversed(newest)), total=total, has_more=total > limit
        )

    def send(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        content: str,
        attachment_ids: tuple[str, ...] = (),
    ) -> tuple[ConversationMessage, ConversationMessage]:
        with self._workspace_operation(workspace, "reply", "正在等待 AI 回复"):
            return self._send(
                workspace,
                model_id=model_id,
                content=content,
                attachment_ids=attachment_ids,
            )

    def retry(
        self,
        workspace: Workspace,
        *,
        error_message_id: str,
        model_id: str,
    ) -> ConversationMessage:
        with self._workspace_operation(workspace, "reply", "正在重新发送消息"):
            self._ensure_workspace_unlocked(workspace)
            model = self._model(model_id)
            error_payload = db.session.scalar(
                select(ConversationMessage.payload).where(
                    ConversationMessage.id == error_message_id,
                    ConversationMessage.workspace_id == workspace.id,
                    ConversationMessage.role == "assistant",
                    ConversationMessage.kind == "error",
                )
            )
            user_message_id = str((error_payload or {}).get("retry_user_message_id", ""))
            user_message = db.session.scalar(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(
                    ConversationMessage.id == user_message_id,
                    ConversationMessage.workspace_id == workspace.id,
                    ConversationMessage.role == "user",
                )
            )
            if user_message is None:
                raise ServiceError(
                    "这条 AI 错误回复无法重新发送",
                    code="conversation_message_not_retryable",
                    status_code=404,
                )
            return self._complete_reply(workspace, model, user_message)

    def _send(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        content: str,
        attachment_ids: tuple[str, ...],
    ) -> tuple[ConversationMessage, ConversationMessage]:
        sent_at = utcnow()
        self._ensure_workspace_unlocked(workspace)
        model = self._model(model_id)
        attachments = self._load_assets(workspace, attachment_ids)
        content = self._validate_message(content, has_attachments=bool(attachments))
        user_message = ConversationMessage(
            workspace_id=workspace.id,
            role="user",
            kind="message",
            content=content,
            payload={},
            provider_id=model.identifier,
            provider_label=model.label,
            model=model.model,
            created_at=sent_at,
        )
        self._attach(user_message, attachments)
        db.session.add(user_message)
        workspace.updated_at = sent_at
        db.session.commit()
        assistant_message = self._complete_reply(
            workspace,
            model,
            user_message,
        )
        return user_message, assistant_message

    def _complete_reply(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        user_message: ConversationMessage,
    ) -> ConversationMessage:
        attachments = [attachment.asset for attachment in user_message.attachments]
        pending = self._user_model_message(user_message.content, attachments)
        try:
            context = self.context.build(
                workspace,
                model,
                client=self.client,
                pending_message=pending,
                pending_stored_message_id=user_message.id,
            )
            db.session.commit()
            result = self.client.complete(
                model,
                system=chat_system_prompt(
                    self.chat_models.system_prompt("chat"),
                    self.chat_models.workspace_prompt(workspace.kind),
                    animation_runtime_prompt(workspace.kind, workspace.settings),
                    generation_prompt=generation_mode_prompt(
                        workspace.kind,
                        "img2img"
                        if workspace.kind == "animation"
                        else str((workspace.settings or {}).get("mode", "text2img")),
                        len(attachments),
                    ),
                ),
                messages=context,
            )
        except OpenAIChatError as exc:
            return self._error_reply(workspace, model, user_message, exc)

        assistant_message = self._assistant_message(
            workspace, model, result, kind="message", payload={}
        )
        db.session.add(assistant_message)
        self._record_chat_success(
            workspace,
            model,
            "chat.reply",
            result,
            details={"attachment_count": len(attachments)},
        )
        self._remember_preferences(workspace, model_id=model.identifier)
        workspace.updated_at = utcnow()
        db.session.commit()
        return assistant_message

    def create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
        mode: str = "",
        reference_ids: tuple[str, ...] = (),
    ) -> ConversationMessage:
        label = (
            "正在检查并总结帧动画需求"
            if workspace.kind == "animation"
            else "正在检查并总结生图需求"
        )
        with self._workspace_operation(workspace, "prompt_draft", label):
            return self._create_prompt_draft(
                workspace,
                model_id=model_id,
                translate_to_english=translate_to_english,
                mode=mode,
                reference_ids=reference_ids,
            )

    def _create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
        mode: str,
        reference_ids: tuple[str, ...],
    ) -> ConversationMessage:
        self._ensure_workspace_unlocked(workspace)
        model = self._model(model_id)
        if not db.session.scalar(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.workspace_id == workspace.id,
                ConversationMessage.role == "user",
            )
            .limit(1)
        ):
            subject = "帧动画" if workspace.kind == "animation" else "图片"
            raise ServiceError(f"请先通过对话描述需要生成的{subject}")
        if mode and mode not in {"text2img", "img2img"}:
            raise ServiceError("生成模式无效")
        if workspace.kind == "animation" and mode and mode != "img2img":
            raise ServiceError("帧动画工作站固定使用一张用户指定的母图")
        requested_mode = (
            "img2img"
            if workspace.kind == "animation"
            else mode or str((workspace.settings or {}).get("mode", "text2img"))
        )
        requested_mode = "img2img" if requested_mode == "img2img" else "text2img"
        if mode:
            attachments = self._load_assets(workspace, reference_ids) if reference_ids else []
            if requested_mode == "text2img" and attachments:
                raise ServiceError("文生图提示词草稿不能携带参考图")
            effective_mode = requested_mode
        else:
            attachments = self._latest_user_attachments(workspace)
            effective_mode = "img2img" if attachments else requested_mode
        if workspace.kind == "animation" and len(attachments) != 1:
            raise ServiceError("帧动画提示词草稿必须且只能选择一张母图")
        request_text = (
            "请基于以上会话整理当前已确认的最终帧动画需求。"
            if workspace.kind == "animation"
            else "请基于以上会话整理当前已确认的最终生图需求。"
        )
        pending = self._user_model_message(request_text, attachments)
        try:
            context = self.context.build(
                workspace,
                model,
                client=self.client,
                pending_message=pending,
            )
            db.session.commit()
            result = self.client.complete(
                model,
                system=PromptDraftParser.system_prompt(
                    translate_to_english=translate_to_english,
                    workspace_kind=workspace.kind,
                    workspace_prompt=self.chat_models.workspace_prompt(workspace.kind),
                    runtime_prompt=animation_runtime_prompt(workspace.kind, workspace.settings),
                    generation_prompt=generation_mode_prompt(
                        workspace.kind,
                        effective_mode,
                        len(attachments),
                    ),
                ),
                messages=context,
                max_output_tokens=min(model.max_output_tokens, 2400),
            )
        except OpenAIChatError as exc:
            self._raise_chat_error(workspace, model, "chat.prompt_draft", exc)
        draft = PromptDraftParser.parse(
            result.content,
            translate_to_english=translate_to_english,
            max_prompt_characters=self.settings.runtime().max_prompt_characters,
        )
        if effective_mode == "img2img" and not attachments:
            draft = {
                "status": "needs_clarification",
                "questions": ["当前目标是参考图生图，请先上传或选择至少一张参考图。"],
                "language": "en" if translate_to_english else "zh",
            }
        draft["generation_mode"] = effective_mode
        draft["reference_ids"] = [asset.id for asset in attachments]
        if draft["status"] == "needs_clarification":
            questions = "\n".join(
                f"{index}. {question}" for index, question in enumerate(draft["questions"], 1)
            )
            content = f"为了让生成结果更符合预期，还需要确认：\n{questions}"
            message_kind = "message"
        else:
            label = (
                "English prompt"
                if translate_to_english
                else ("帧动画提示词" if workspace.kind == "animation" else "生图提示词")
            )
            content = f"需求确认\n{draft['summary_zh']}\n\n{label}\n{draft['prompt']}"
            message_kind = "prompt_draft"
        message = self._assistant_message(
            workspace,
            model,
            ChatCompletion(
                content=content,
                request_id=result.request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                elapsed_seconds=result.elapsed_seconds,
            ),
            kind=message_kind,
            payload=draft,
        )
        self._attach(message, attachments)
        db.session.add(message)
        self._record_chat_success(
            workspace,
            model,
            "chat.prompt_draft",
            result,
            details={
                "attachment_count": len(attachments),
                "translate_to_english": translate_to_english,
                "outcome": draft["status"],
                "generation_mode": effective_mode,
                "reference_count": len(attachments),
            },
        )
        self._remember_preferences(
            workspace,
            model_id=model.identifier,
            translate_to_english=translate_to_english,
        )
        workspace.updated_at = utcnow()
        db.session.commit()
        return message

    def state_dict(self, workspace: Workspace) -> dict[str, Any]:
        state = db.session.get(ConversationState, workspace.id)
        return {
            "compacted": bool(state and state.summary),
            "estimated_context_tokens": state.estimated_context_tokens if state else 0,
            "max_context_tokens": self.chat_models.context.max_context_tokens,
        }

    def operation_state(self, workspace_id: str) -> dict[str, Any]:
        with self._operation_lock:
            operation = self._operations.get(workspace_id)
        if operation is None:
            return {"busy": False, "kind": "", "label": "", "started_at": None}
        return operation.public_dict()

    @contextmanager
    def generation_submission(self, workspace: Workspace) -> Iterator[None]:
        with self._workspace_operation(
            workspace,
            "generation_submission",
            "正在提交生成任务",
            enforce_chat_capacity=False,
        ):
            yield

    @contextmanager
    def workspace_mutation(self, workspace: Workspace, label: str) -> Iterator[None]:
        with self._workspace_operation(
            workspace,
            "workspace_mutation",
            label,
            enforce_chat_capacity=False,
        ):
            yield

    @contextmanager
    def _workspace_operation(
        self,
        workspace: Workspace,
        kind: str,
        label: str,
        *,
        enforce_chat_capacity: bool = True,
    ) -> Iterator[None]:
        if enforce_chat_capacity:
            runtime = self.settings.runtime()
        operation = ConversationOperation(
            user_id=workspace.user_id,
            kind=kind,
            label=label,
            started_at=utcnow(),
        )
        with self._operation_lock:
            active = self._operations.get(workspace.id)
            if active is not None:
                raise self._busy_error(active)
            if enforce_chat_capacity:
                chat_operations = tuple(
                    active
                    for active in self._operations.values()
                    if active.kind not in {"generation_submission", "workspace_mutation"}
                )
                user_operations = sum(
                    active.user_id == workspace.user_id for active in chat_operations
                )
                if user_operations >= runtime.max_concurrent_chats_per_user:
                    raise ServiceError(
                        f"同一账户最多同时进行 {runtime.max_concurrent_chats_per_user} 个 AI 对话请求",
                        code="conversation_user_limit",
                        status_code=429,
                    )
                if len(chat_operations) >= runtime.max_concurrent_chats:
                    raise ServiceError(
                        "当前 AI 对话请求较多，请稍后重试",
                        code="conversation_capacity",
                        status_code=503,
                    )
            self._operations[workspace.id] = operation
        try:
            yield
        finally:
            with self._operation_lock:
                if self._operations.get(workspace.id) is operation:
                    self._operations.pop(workspace.id, None)

    @staticmethod
    def _busy_error(operation: ConversationOperation) -> ServiceError:
        return ServiceError(
            f"{operation.label}，请完成后再继续",
            code="conversation_busy",
            status_code=409,
        )

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

    def _latest_user_attachments(self, workspace: Workspace) -> list[Asset]:
        messages = list(
            db.session.scalars(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(
                    ConversationMessage.workspace_id == workspace.id,
                    ConversationMessage.role == "user",
                )
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            )
        )
        for message in messages:
            if message.attachments:
                return [attachment.asset for attachment in message.attachments]
        return []

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
    ) -> None:
        settings = dict(workspace.settings or {})
        settings["chat_model_id"] = model_id
        if translate_to_english is not None:
            settings["translate_prompt"] = translate_to_english
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
                "当前帧动画尚未生成完成，请等待完成或先取消任务"
                if workspace.kind == "animation"
                else "当前图片尚未生成完成，请等待完成或先取消任务",
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

    def _error_reply(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        user_message: ConversationMessage,
        error: OpenAIChatError,
    ) -> ConversationMessage:
        error_id = self._record_chat_error(workspace, model, "chat.reply", error)
        content = str(error)
        if error_id:
            content = f"{content}\n错误 ID：{error_id}"
        message = ConversationMessage(
            workspace_id=workspace.id,
            role="assistant",
            kind="error",
            content=content,
            payload={
                "code": error.code,
                "error_id": error_id,
                "retry_user_message_id": user_message.id,
            },
            provider_id=model.identifier,
            provider_label=model.label,
            model=model.model,
            upstream_request_id=error.request_id,
            elapsed_seconds=(
                None if error.elapsed_seconds is None else round(error.elapsed_seconds, 3)
            ),
        )
        db.session.add(message)
        workspace.updated_at = utcnow()
        db.session.commit()
        return message

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
