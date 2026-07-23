from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...config.chat_models import ChatModelConfig
from ...errors import ServiceError
from ...extensions import db
from ...integrations.openai_chat import OpenAIChatError
from ...models import (
    Asset,
    ConversationAttachment,
    ConversationMessage,
    Workspace,
    new_public_id,
    utcnow,
)
from ..prompt_drafts import PromptDraftReview
from .clarifications import ClarificationReferenceResolver
from .operations import ConversationOperation, ConversationOperationRegistry
from .prompts import generation_mode_prompt
from .support import ConversationDependencies, ConversationSupport


class ConversationReplyService(ConversationSupport):
    def __init__(
        self,
        dependencies: ConversationDependencies,
        operations: ConversationOperationRegistry,
    ):
        super().__init__(dependencies)
        self.operations = operations
        self.clarifications = ClarificationReferenceResolver(self._load_assets)

    def send(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        content: str,
        attachment_ids: tuple[str, ...] = (),
        generation_reference_ids: tuple[str, ...] = (),
        generation_mode: str = "",
        clarification_reply_to_id: str = "",
        message_id: str = "",
        operation_id: str = "",
    ) -> tuple[ConversationMessage, ConversationMessage]:
        message_id = self._message_id(message_id or new_public_id())
        operation_id = self._message_id(operation_id or new_public_id())
        with self.operations.workspace_operation(
            workspace,
            "reply",
            "正在确认需求",
            operation_id=operation_id,
            message_id=message_id,
        ) as operation:
            return self._send(
                workspace,
                model_id=model_id,
                content=content,
                attachment_ids=attachment_ids,
                generation_reference_ids=generation_reference_ids,
                generation_mode=generation_mode,
                clarification_reply_to_id=clarification_reply_to_id,
                message_id=message_id,
                operation=operation,
            )

    def retry(
        self,
        workspace: Workspace,
        *,
        error_message_id: str,
        model_id: str,
        operation_id: str = "",
    ) -> ConversationMessage:
        operation_id = self._message_id(operation_id or new_public_id())
        with self.operations.workspace_operation(
            workspace,
            "reply",
            "正在重新确认需求",
            operation_id=operation_id,
            message_id=error_message_id,
        ) as operation:
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
            current_reply = self._linked_reply(workspace, user_message)
            if current_reply is not None and current_reply.id != error_message_id:
                return current_reply
            self._ensure_workspace_unlocked(workspace)
            model = self._model(model_id)
            return self._complete_reply(
                workspace,
                model,
                user_message,
                operation=operation,
            )

    def _send(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        content: str,
        attachment_ids: tuple[str, ...],
        generation_reference_ids: tuple[str, ...],
        generation_mode: str,
        clarification_reply_to_id: str,
        message_id: str,
        operation: ConversationOperation,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        sent_at = utcnow()
        content = self._validate_message(content, has_attachments=bool(attachment_ids))
        requested_mode = str(generation_mode or "").strip().lower()
        if not requested_mode:
            configured_mode = str((workspace.settings or {}).get("mode", "text2img"))
            requested_mode = (
                "auto" if attachment_ids and configured_mode != "img2img" else configured_mode
            )
        generation_mode = self._normalize_generation_mode(requested_mode)
        clarification_reply_to_id = str(clarification_reply_to_id or "").strip().lower()
        if clarification_reply_to_id:
            clarification_reply_to_id = self._message_id(clarification_reply_to_id)
            self.clarifications.ensure_open(workspace, clarification_reply_to_id)
        if generation_mode == "text2img" and generation_reference_ids:
            raise ServiceError("文生图消息不能携带垫图")
        if generation_mode == "auto" and generation_reference_ids:
            raise ServiceError("自动判断模式不能预先指定垫图")
        existing = self._matching_user_message(
            workspace,
            message_id=message_id,
            model_id=model_id,
            content=content,
            attachment_ids=attachment_ids,
            generation_reference_ids=generation_reference_ids,
            generation_mode=generation_mode,
            clarification_reply_to_id=clarification_reply_to_id,
        )
        if existing is not None:
            reply = self._linked_reply(workspace, existing)
            if reply is not None:
                return existing, reply
            self._ensure_workspace_unlocked(workspace)
            model = self._model(model_id)
            return existing, self._complete_reply(
                workspace,
                model,
                existing,
                operation=operation,
            )

        operation.ensure_active()
        self._ensure_workspace_unlocked(workspace)
        model = self._model(model_id)
        attachments = self._load_assets(workspace, attachment_ids)
        generation_references = self._load_assets(workspace, generation_reference_ids)
        user_message = ConversationMessage(
            id=message_id,
            workspace_id=workspace.id,
            role="user",
            kind="message",
            content=content,
            payload={
                "generation_mode": generation_mode,
                "generation_reference_ids": [asset.id for asset in generation_references],
                "clarification_reply_to_id": clarification_reply_to_id,
            },
            provider_id=model.identifier,
            provider_label=model.label,
            model=model.model,
            created_at=sent_at,
        )
        self._attach(user_message, attachments)
        db.session.add(user_message)
        workspace.settings = {**(workspace.settings or {}), "prompt_draft_id": ""}
        workspace.updated_at = sent_at
        db.session.commit()
        assistant_message = self._complete_reply(
            workspace,
            model,
            user_message,
            operation=operation,
        )
        return user_message, assistant_message

    def _complete_reply(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        user_message: ConversationMessage,
        *,
        operation: ConversationOperation,
    ) -> ConversationMessage:
        operation.ensure_active()
        attachments = [attachment.asset for attachment in user_message.attachments]
        payload = user_message.payload or {}
        generation_reference_ids = tuple(
            str(item) for item in payload.get("generation_reference_ids", [])
        )
        generation_references = (
            self._load_assets(workspace, generation_reference_ids)
            if generation_reference_ids
            else []
        )
        mode = self._stored_generation_mode(
            workspace,
            payload,
            attachments=attachments,
            generation_reference_ids=generation_reference_ids,
        )
        if mode == "img2img":
            candidate_references = generation_references
            review_mode = "img2img"
        elif mode == "auto":
            candidate_references = attachments
            review_mode = "auto"
        else:
            candidate_references = []
            review_mode = "text2img"
        clarification_reply_to_id = str(payload.get("clarification_reply_to_id", "")).strip()
        if not candidate_references and clarification_reply_to_id:
            inherited = self.clarifications.resolve(workspace, clarification_reply_to_id)
            if inherited is not None:
                candidate_references, review_mode = inherited
        series_anchor = self._active_series_anchor(workspace)
        if series_anchor:
            candidate_references = self._with_series_anchor(
                workspace,
                candidate_references,
                series_anchor,
            )
            review_mode = "img2img"
        review_candidates = candidate_references if review_mode != "text2img" else attachments
        context_attachments = self._merge_context_assets(
            workspace,
            [*review_candidates, *attachments],
        )
        pending = self._user_model_message(user_message.content, context_attachments)
        settings = workspace.settings or {}
        direction_id = str(settings.get("creative_direction_id", "auto"))
        retrieval = self._creative_matches(
            workspace,
            direction_id=direction_id,
        )
        review = PromptDraftReview(
            translate_to_english=settings.get("translate_prompt") is True,
            workspace_prompt=self.chat_models.workspace_prompt(workspace.kind),
            conversation_prompt=self.chat_models.system_prompt("chat"),
            generation_prompt=generation_mode_prompt(
                review_mode,
                len(candidate_references),
            ),
            generation_mode=review_mode,
            creative_direction_id=direction_id,
            max_prompt_characters=self.settings.runtime().max_prompt_characters,
            reference_count=len(candidate_references),
            template_candidates=retrieval.templates,
            retrieved_cases=retrieval.cases,
            retrieval_confidence=retrieval.confidence,
            retrieval_reason=retrieval.reason,
            active_series_contract=series_anchor.anchor.contract if series_anchor else {},
        )
        try:
            context = self.context.build(
                workspace,
                pending_message=pending,
                pending_stored_message_id=user_message.id,
                pending_image_keys=(f"asset:{asset.id}" for asset in context_attachments),
            )
            db.session.commit()
            operation.ensure_active()
            result = self.client.complete(
                model,
                system=review.system_prompt(),
                messages=context,
                max_output_tokens=min(model.max_output_tokens, 2400),
                reasoning_effort=model.effective_review_reasoning_effort,
            )
        except OpenAIChatError as exc:
            return self._error_reply(
                workspace,
                model,
                user_message,
                exc,
                operation=operation,
            )

        operation.ensure_active()
        try:
            draft = review.finalize(
                review.parse(result.content),
                generation_mode=review_mode,
                reference_ids=[asset.id for asset in review_candidates],
            )
        except ServiceError as exc:
            return self._error_reply(
                workspace,
                model,
                user_message,
                self._structured_output_error(exc, result),
                operation=operation,
            )
        if draft.get("status") == "needs_clarification" and candidate_references:
            # Keep pad images on open clarifications so follow-up answers inherit them.
            draft["reference_ids"] = [asset.id for asset in candidate_references]
            if draft.get("generation_mode") not in {"img2img", "auto"}:
                draft["generation_mode"] = "img2img" if review_mode == "img2img" else "auto"
        reply_content, message_kind = review.message_content(draft)
        payload = {**draft, "reply_to_message_id": user_message.id}
        generation_references = self._draft_references(draft, candidate_references)
        if draft.get("status") == "needs_clarification" and not generation_references:
            generation_references = list(candidate_references)
        assistant_message = self._assistant_message(
            workspace,
            model,
            replace(result, content=reply_content),
            kind=message_kind,
            payload=payload,
        )
        if message_kind == "prompt_draft" or (
            draft.get("status") == "needs_clarification" and generation_references
        ):
            self._attach(assistant_message, generation_references)
        operation.ensure_active()
        db.session.add(assistant_message)
        user_message.payload["reply_message_id"] = assistant_message.id
        self._record_chat_success(
            workspace,
            model,
            "chat.reply",
            result,
            details={
                "attachment_count": len(attachments),
                "outcome": draft["status"],
                "generation_mode": draft["generation_mode"],
                "reference_count": len(generation_references),
                "reference_usage": draft["reference_usage"],
                "retrieved_case_count": len(draft.get("retrieved_cases", [])),
                "template_candidate_count": len(retrieval.templates),
            },
        )
        self._remember_preferences(workspace, model_id=model.identifier)
        workspace.updated_at = utcnow()
        operation.ensure_active()
        db.session.commit()
        return assistant_message

    @staticmethod
    def _normalize_generation_mode(value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode not in {"", "auto", "text2img", "img2img"}:
            raise ServiceError("生成模式无效")
        return mode or "text2img"

    def _stored_generation_mode(
        self,
        workspace: Workspace,
        payload: dict[str, Any],
        *,
        attachments: list[Asset],
        generation_reference_ids: tuple[str, ...],
    ) -> str:
        raw_mode = str(payload.get("generation_mode", "")).strip().lower()
        if raw_mode:
            return self._normalize_generation_mode(raw_mode)
        if generation_reference_ids:
            return "img2img"
        configured_mode = str((workspace.settings or {}).get("mode", "text2img"))
        if configured_mode == "img2img":
            return "img2img"
        return "auto" if attachments else "text2img"

    def _matching_user_message(
        self,
        workspace: Workspace,
        *,
        message_id: str,
        model_id: str,
        content: str,
        attachment_ids: tuple[str, ...],
        generation_reference_ids: tuple[str, ...],
        generation_mode: str,
        clarification_reply_to_id: str,
    ) -> ConversationMessage | None:
        message = db.session.scalar(
            select(ConversationMessage)
            .options(
                selectinload(ConversationMessage.attachments).selectinload(
                    ConversationAttachment.asset
                )
            )
            .where(ConversationMessage.id == message_id)
        )
        if message is None:
            return None
        if (
            message.workspace_id != workspace.id
            or message.role != "user"
            or message.provider_id != model_id
            or message.content != content
            or tuple(attachment.asset_id for attachment in message.attachments) != attachment_ids
            or tuple(
                str(item) for item in (message.payload or {}).get("generation_reference_ids", [])
            )
            != generation_reference_ids
            or str((message.payload or {}).get("generation_mode", "text2img")) != generation_mode
            or str((message.payload or {}).get("clarification_reply_to_id", ""))
            != clarification_reply_to_id
        ):
            raise ServiceError(
                "消息 ID 已被其他内容使用",
                code="conversation_message_id_conflict",
                status_code=409,
            )
        return message

    @staticmethod
    def _linked_reply(
        workspace: Workspace,
        user_message: ConversationMessage,
    ) -> ConversationMessage | None:
        reply_id = str((user_message.payload or {}).get("reply_message_id", ""))
        if not reply_id:
            return None
        reply = db.session.get(ConversationMessage, reply_id)
        if (
            reply is None
            or reply.workspace_id != workspace.id
            or reply.role != "assistant"
            or str((reply.payload or {}).get("reply_to_message_id", "")) != user_message.id
        ):
            raise ServiceError(
                "AI 回复关联无效",
                code="conversation_message_id_conflict",
                status_code=409,
            )
        return reply

    def _error_reply(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        user_message: ConversationMessage,
        error: OpenAIChatError,
        *,
        operation: ConversationOperation,
    ) -> ConversationMessage:
        operation.ensure_active()
        error_id = self._record_chat_error(workspace, model, "chat.reply", error)
        content = str(error)
        if error_id:
            content = f"{content}\n错误 ID：{error_id}"
        message = ConversationMessage(
            id=new_public_id(),
            workspace_id=workspace.id,
            role="assistant",
            kind="error",
            content=content,
            payload={
                "code": error.code,
                "error_id": error_id,
                "retry_user_message_id": user_message.id,
                "reply_to_message_id": user_message.id,
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
        user_message.payload["reply_message_id"] = message.id
        workspace.updated_at = utcnow()
        operation.ensure_active()
        db.session.commit()
        return message
