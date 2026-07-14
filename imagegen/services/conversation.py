from __future__ import annotations

import base64
import re
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
from .conversation_prompts import CHAT_SYSTEM_PROMPT
from .prompt_drafts import PromptDraftParser


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
    MAX_MESSAGE_LENGTH = 12000
    MAX_ATTACHMENTS = 8
    MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
    MAX_ATTACHMENT_TOTAL_BYTES = 40 * 1024 * 1024
    MAX_CONCURRENT_OPERATIONS = 4
    MAX_CONCURRENT_PER_USER = 2

    def __init__(
        self,
        chat_models: ChatModelRegistry,
        storage: ImageStorage,
        client: OpenAIChatClient | None = None,
    ):
        self.chat_models = chat_models
        self.storage = storage
        self.client = client or OpenAIChatClient()
        self.context = ConversationContextManager(chat_models, self.client)
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
        first_user_message = not db.session.scalar(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.workspace_id == workspace.id,
                ConversationMessage.role == "user",
            )
            .limit(1)
        )
        pending = self._user_model_message(content, attachments)
        try:
            context = self.context.build(
                workspace,
                model,
                pending_message=pending,
                pending_text=content,
                pending_image_count=len(attachments),
            )
            db.session.commit()
            result = self.client.complete(model, system=CHAT_SYSTEM_PROMPT, messages=context)
        except OpenAIChatError as exc:
            self._raise_chat_error(exc)

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
        assistant_message = self._assistant_message(
            workspace, model, result, kind="message", payload={}
        )
        db.session.add_all([user_message, assistant_message])
        self._remember_preferences(workspace, model_id=model.identifier)
        if first_user_message and bool((workspace.settings or {}).get("auto_title", True)):
            self._set_automatic_title(workspace, content)
        workspace.updated_at = utcnow()
        db.session.commit()
        return user_message, assistant_message

    def create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
    ) -> ConversationMessage:
        with self._workspace_operation(workspace, "prompt_draft", "正在整理生图提示词"):
            return self._create_prompt_draft(
                workspace,
                model_id=model_id,
                translate_to_english=translate_to_english,
            )

    def _create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
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
            raise ServiceError("请先通过对话描述需要生成的图片")
        attachments = self._latest_user_attachments(workspace)
        request_text = "请基于以上会话整理当前已确认的最终生图需求。"
        pending = self._user_model_message(request_text, attachments)
        try:
            context = self.context.build(
                workspace,
                model,
                pending_message=pending,
                pending_text=request_text,
                pending_image_count=len(attachments),
            )
            db.session.commit()
            result = self.client.complete(
                model,
                system=PromptDraftParser.system_prompt(translate_to_english=translate_to_english),
                messages=context,
                max_output_tokens=min(model.max_output_tokens, 2400),
            )
        except OpenAIChatError as exc:
            self._raise_chat_error(exc)
        draft = PromptDraftParser.parse(result.content, translate_to_english=translate_to_english)
        draft["reference_ids"] = [asset.id for asset in attachments]
        label = "English prompt" if translate_to_english else "生图提示词"
        message = self._assistant_message(
            workspace,
            model,
            ChatCompletion(
                content=f"需求确认\n{draft['summary_zh']}\n\n{label}\n{draft['prompt']}",
                request_id=result.request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                elapsed_seconds=result.elapsed_seconds,
            ),
            kind="prompt_draft",
            payload=draft,
        )
        self._attach(message, attachments)
        db.session.add(message)
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

    def ensure_idle(self, workspace_id: str) -> None:
        with self._operation_lock:
            operation = self._operations.get(workspace_id)
        if operation is not None:
            raise self._busy_error(operation)

    @contextmanager
    def _workspace_operation(self, workspace: Workspace, kind: str, label: str) -> Iterator[None]:
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
            user_operations = sum(
                active.user_id == workspace.user_id for active in self._operations.values()
            )
            if user_operations >= self.MAX_CONCURRENT_PER_USER:
                raise ServiceError(
                    f"同一账户最多同时进行 {self.MAX_CONCURRENT_PER_USER} 个 AI 对话请求",
                    code="conversation_user_limit",
                    status_code=429,
                )
            if len(self._operations) >= self.MAX_CONCURRENT_OPERATIONS:
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
        if len(asset_ids) != len(set(asset_ids)):
            raise ServiceError("参考图不能重复")
        if len(asset_ids) > self.MAX_ATTACHMENTS:
            raise ServiceError(f"单条消息最多附加 {self.MAX_ATTACHMENTS} 张参考图")
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
        if any(asset.byte_count > self.MAX_ATTACHMENT_BYTES for asset in ordered):
            raise ServiceError("单张参考图不能超过 10 MiB")
        if sum(asset.byte_count for asset in ordered) > self.MAX_ATTACHMENT_TOTAL_BYTES:
            raise ServiceError("参考图合计不能超过 40 MiB")
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

    @staticmethod
    def _validate_message(content: str, *, has_attachments: bool) -> str:
        content = content.strip()
        if not content and has_attachments:
            content = "请分析这些参考图，帮助我明确生图需求。"
        if not content:
            raise ServiceError("请输入消息")
        if len(content) > ConversationService.MAX_MESSAGE_LENGTH:
            raise ServiceError(f"单条消息不能超过 {ConversationService.MAX_MESSAGE_LENGTH} 个字符")
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
                "当前图片尚未生成完成，请等待完成或先取消任务",
                code="workspace_generation_active",
                status_code=409,
            )

    @staticmethod
    def _set_automatic_title(workspace: Workspace, content: str) -> None:
        base = re.sub(r"\s+", " ", content).strip()[:36] or "新会话"
        candidate = base
        suffix = 2
        while db.session.scalar(
            select(Workspace.id)
            .where(
                Workspace.user_id == workspace.user_id,
                Workspace.id != workspace.id,
                func.lower(Workspace.name) == candidate.lower(),
            )
            .limit(1)
        ):
            marker = f" {suffix}"
            candidate = f"{base[: 36 - len(marker)]}{marker}"
            suffix += 1
        workspace.name = candidate

    @staticmethod
    def _raise_chat_error(error: OpenAIChatError) -> None:
        db.session.rollback()
        raise ServiceError(str(error), code=error.code, status_code=error.status_code) from error
