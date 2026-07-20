from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ...errors import ServiceError
from ...extensions import db
from ...integrations.openai_chat import ChatCompletion, OpenAIChatError
from ...models import Asset, ConversationMessage, Workspace, utcnow
from ..creative import get_creative_direction
from ..prompt_drafts import PromptDraftReview
from .operations import ConversationOperationRegistry
from .prompts import generation_mode_prompt
from .support import ConversationDependencies, ConversationSupport


class PromptDraftWorkflow(ConversationSupport):
    def __init__(
        self,
        dependencies: ConversationDependencies,
        operations: ConversationOperationRegistry,
    ):
        super().__init__(dependencies)
        self.operations = operations

    def create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
        mode: str = "",
        reference_ids: tuple[str, ...] = (),
        creative_direction_id: str = "auto",
    ) -> ConversationMessage:
        with self.operations.workspace_operation(
            workspace, "prompt_draft", "正在检查并总结生图需求"
        ):
            return self._create_prompt_draft(
                workspace,
                model_id=model_id,
                translate_to_english=translate_to_english,
                mode=mode,
                reference_ids=reference_ids,
                creative_direction_id=creative_direction_id,
            )

    def _create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
        mode: str,
        reference_ids: tuple[str, ...],
        creative_direction_id: str,
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
        effective_mode, attachments = self._prompt_draft_inputs(
            workspace,
            mode=mode,
            reference_ids=reference_ids,
            creative_direction_id=creative_direction_id,
        )
        pending = self._user_model_message(
            "请基于以上会话整理当前已确认的最终生图需求。", attachments
        )
        review = PromptDraftReview(
            translate_to_english=translate_to_english,
            workspace_prompt=self.chat_models.workspace_prompt(workspace.kind),
            generation_prompt=generation_mode_prompt(
                effective_mode,
                len(attachments),
            ),
            creative_direction_id=creative_direction_id,
            max_prompt_characters=self.settings.runtime().max_prompt_characters,
        )
        try:
            context = self.context.build(
                workspace,
                pending_message=pending,
                pending_image_keys=(f"asset:{asset.id}" for asset in attachments),
            )
            db.session.commit()
            result = self.client.complete(
                model,
                system=review.system_prompt(),
                messages=context,
                max_output_tokens=min(model.max_output_tokens, 2400),
            )
        except OpenAIChatError as exc:
            self._raise_chat_error(workspace, model, "chat.prompt_draft", exc)
        draft = review.finalize(
            review.parse(result.content),
            generation_mode=effective_mode,
            reference_ids=[asset.id for asset in attachments],
        )
        generation_references = self._draft_references(draft, attachments)
        content, message_kind = review.message_content(draft)
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
        self._attach(message, generation_references)
        db.session.add(message)
        self._record_chat_success(
            workspace,
            model,
            "chat.prompt_draft",
            result,
            details={
                "attachment_count": len(generation_references),
                "translate_to_english": translate_to_english,
                "outcome": draft["status"],
                "generation_mode": draft["generation_mode"],
                "reference_count": len(generation_references),
                "reference_usage": draft["reference_usage"],
                "creative_direction": draft.get("creative_direction", "other"),
                "template_id": draft.get("template_id", "custom"),
            },
        )
        self._remember_preferences(
            workspace,
            model_id=model.identifier,
            translate_to_english=translate_to_english,
            creative_direction_id=creative_direction_id,
        )
        workspace.updated_at = utcnow()
        db.session.commit()
        return message

    def _prompt_draft_inputs(
        self,
        workspace: Workspace,
        *,
        mode: str,
        reference_ids: tuple[str, ...],
        creative_direction_id: str,
    ) -> tuple[str, list[Asset]]:
        if mode and mode not in {"text2img", "img2img"}:
            raise ServiceError("生成模式无效")
        try:
            get_creative_direction(creative_direction_id)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        requested_mode = mode or str((workspace.settings or {}).get("mode", "text2img"))
        requested_mode = "img2img" if requested_mode == "img2img" else "text2img"
        attachments = self._load_assets(workspace, reference_ids) if reference_ids else []
        if not mode and attachments and requested_mode == "text2img":
            requested_mode = "img2img"
        if requested_mode == "text2img" and attachments:
            raise ServiceError("文生图提示词草稿不能携带参考图")
        return requested_mode, attachments

    def validate_generation_draft(
        self,
        workspace: Workspace,
        *,
        draft_id: str,
        prompt: str,
        mode: str,
        reference_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        draft = db.session.get(ConversationMessage, self._message_id(draft_id))
        if (
            draft is None
            or draft.workspace_id != workspace.id
            or draft.role != "assistant"
            or draft.kind != "prompt_draft"
        ):
            raise ServiceError(
                "请先使用 AI 整理当前需求并应用最终提示词",
                code="prompt_review_required",
                status_code=409,
            )
        newer_user_message = db.session.scalar(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.workspace_id == workspace.id,
                ConversationMessage.role == "user",
                ConversationMessage.created_at > draft.created_at,
            )
            .limit(1)
        )
        if newer_user_message is not None:
            raise ServiceError(
                "需求已有新的对话，请重新整理最终提示词",
                code="prompt_review_stale",
                status_code=409,
            )
        payload = draft.payload or {}
        if (
            payload.get("status") != "ready"
            or str(payload.get("prompt", "")).strip() != prompt.strip()
        ):
            raise ServiceError(
                "提示词已改变，请重新整理最终提示词",
                code="prompt_review_stale",
                status_code=409,
            )
        if str(payload.get("generation_mode", "")) != mode:
            raise ServiceError(
                "生成模式已改变，请重新整理最终提示词",
                code="prompt_review_stale",
                status_code=409,
            )
        reviewed_references = tuple(str(item) for item in payload.get("reference_ids", []))
        if reviewed_references != reference_ids:
            raise ServiceError(
                "参考图或顺序已改变，请重新整理最终提示词",
                code="prompt_review_stale",
                status_code=409,
            )
        return payload
